import os
import secrets
from pathlib import Path


def _data_dir() -> Path:
    p = Path(os.environ.get("LOCK_DATA_DIR", "data")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    (p / "photos").mkdir(exist_ok=True)
    (p / "thumbs").mkdir(exist_ok=True)
    return p


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "vault.db"
PHOTO_DIR = DATA_DIR / "photos"
THUMB_DIR = DATA_DIR / "thumbs"

SESSION_TTL_SECONDS = int(os.environ.get("LOCK_SESSION_TTL", "1800"))
SESSION_COOKIE = "lock_session"

# Cookie signing key. Persisted across restarts so existing cookies don't
# linger as garbage, but rotated cleanly if the file is deleted. This signs
# the session id only; it does NOT protect any encryption key.
_SIGNING_KEY_PATH = DATA_DIR / "cookie_signing.key"
if _SIGNING_KEY_PATH.exists():
    SIGNING_KEY = _SIGNING_KEY_PATH.read_bytes()
else:
    SIGNING_KEY = secrets.token_bytes(32)
    _SIGNING_KEY_PATH.write_bytes(SIGNING_KEY)
    os.chmod(_SIGNING_KEY_PATH, 0o600)

VAULT_ISSUER = os.environ.get("LOCK_ISSUER", "Lock Vault")
MAX_UPLOAD_BYTES = int(os.environ.get("LOCK_MAX_UPLOAD", str(50 * 1024 * 1024)))
THUMBNAIL_SIZE = (320, 320)
