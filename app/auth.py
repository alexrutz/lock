import io
import secrets
from base64 import b64encode

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer

from . import crypto
from .config import SESSION_COOKIE, SIGNING_KEY, VAULT_ISSUER
from .db import get_conn, vault_initialized
from .sessions import store

_hasher = PasswordHasher()
_serializer = URLSafeSerializer(SIGNING_KEY, salt="lock-session")


def _sign(sid: str) -> str:
    return _serializer.dumps(sid)


def _unsign(token: str) -> str | None:
    try:
        return _serializer.loads(token)
    except BadSignature:
        return None


def read_session_key(request: Request) -> bytes | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sid = _unsign(token)
    if not sid:
        return None
    return store.get_key(sid)


def require_key(request: Request) -> bytes:
    key = read_session_key(request)
    if key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return key


def make_session_cookie(master_key: bytes) -> tuple[str, str]:
    sid = store.create(master_key)
    return SESSION_COOKIE, _sign(sid)


def destroy_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return
    sid = _unsign(token)
    store.destroy(sid)


def setup_vault(password: str) -> tuple[str, str]:
    """Initialize the vault. Returns (totp_secret, qr_data_uri)."""
    if vault_initialized():
        raise HTTPException(status_code=409, detail="Vault already initialized")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    salt = crypto.new_salt()
    master_key = crypto.derive_key(password, salt)
    password_verifier = _hasher.hash(password)

    totp_secret = pyotp.random_base32()
    totp_nonce, totp_ct = crypto.encrypt(master_key, totp_secret.encode("ascii"))
    sentinel_nonce, sentinel_ct = crypto.encrypt(master_key, crypto.SENTINEL)

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO vault_config
               (id, kdf_salt, password_verifier, totp_secret_ct, totp_nonce,
                sentinel_ct, sentinel_nonce)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (salt, password_verifier, totp_ct, totp_nonce, sentinel_ct, sentinel_nonce),
        )

    uri = pyotp.totp.TOTP(totp_secret).provisioning_uri(
        name="vault", issuer_name=VAULT_ISSUER
    )
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + b64encode(buf.getvalue()).decode("ascii")
    return totp_secret, qr_data_uri


def authenticate(password: str, totp_code: str) -> bytes:
    """Verify password + TOTP, return the derived master key.

    Raises HTTPException(401) on any failure, with a generic message so we
    don't leak which factor was wrong.
    """
    generic = HTTPException(status_code=401, detail="Invalid credentials")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT kdf_salt, password_verifier, totp_secret_ct, totp_nonce, "
            "sentinel_ct, sentinel_nonce FROM vault_config WHERE id = 1"
        ).fetchone()
    if row is None:
        raise generic

    try:
        _hasher.verify(row["password_verifier"], password)
    except VerifyMismatchError:
        # Run KDF anyway to keep timing roughly constant.
        crypto.derive_key(password, row["kdf_salt"])
        raise generic

    master_key = crypto.derive_key(password, row["kdf_salt"])

    try:
        sentinel = crypto.decrypt(master_key, row["sentinel_nonce"], row["sentinel_ct"])
        if sentinel != crypto.SENTINEL:
            raise generic
        totp_secret = crypto.decrypt(
            master_key, row["totp_nonce"], row["totp_secret_ct"]
        ).decode("ascii")
    except InvalidTag:
        raise generic

    totp = pyotp.TOTP(totp_secret)
    if not totp.verify(totp_code.strip(), valid_window=1):
        raise generic

    return master_key


def constant_time_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())
