import secrets
import threading
import time
from dataclasses import dataclass

from .config import SESSION_TTL_SECONDS


@dataclass
class _Session:
    master_key: bytes
    expires_at: float


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

    def get_key(self, sid: str | None) -> bytes | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            now = time.time()
            if s.expires_at < now:
                del self._sessions[sid]
                return None
            s.expires_at = now + SESSION_TTL_SECONDS
            return s.master_key

    def destroy(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)


store = SessionStore()
