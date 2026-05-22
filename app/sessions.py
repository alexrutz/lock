import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .config import SESSION_TTL_SECONDS, THUMB_CACHE_ENTRIES, NAME_CACHE_ENTRIES


class _LRU:
    """Tiny thread-safe LRU. Values are bytes-or-str; we cap entry count, not size."""

    def __init__(self, max_items: int) -> None:
        self._d: "OrderedDict[int, object]" = OrderedDict()
        self._max = max_items
        self._lock = threading.Lock()

    def get(self, key: int):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key: int, value) -> None:
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
            self._d[key] = value
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    def pop(self, key: int) -> None:
        with self._lock:
            self._d.pop(key, None)


@dataclass
class _Session:
    master_key: bytes
    expires_at: float
    thumb_cache: _LRU = field(default_factory=lambda: _LRU(THUMB_CACHE_ENTRIES))
    name_cache: _LRU = field(default_factory=lambda: _LRU(NAME_CACHE_ENTRIES))
    # Decrypted EXIF descriptions, keyed by photo id. Used by search.
    description_cache: _LRU = field(default_factory=lambda: _LRU(NAME_CACHE_ENTRIES))
    # Tag names — typically a few dozen entries, small strings.
    tag_name_cache: _LRU = field(default_factory=lambda: _LRU(2048))


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def create(self, master_key: bytes) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = _Session(
                master_key=master_key,
                expires_at=time.time() + SESSION_TTL_SECONDS,
            )
        return sid

    def _touch(self, s: _Session) -> None:
        s.expires_at = time.time() + SESSION_TTL_SECONDS

    def get_key(self, sid: str | None) -> bytes | None:
        s = self._get(sid)
        return None if s is None else s.master_key

    def get_session(self, sid: str | None) -> _Session | None:
        return self._get(sid)

    def _get(self, sid: str | None) -> _Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if s.expires_at < time.time():
                del self._sessions[sid]
                return None
            self._touch(s)
            return s

    def destroy(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)

    def evict_photo(self, photo_id: int) -> None:
        """Drop a photo from all session caches (called after delete)."""
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.thumb_cache.pop(photo_id)
            s.name_cache.pop(photo_id)
            s.description_cache.pop(photo_id)


store = SessionStore()
