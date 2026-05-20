import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kdf_salt BLOB NOT NULL,
    password_verifier TEXT NOT NULL,
    totp_secret_ct BLOB NOT NULL,
    totp_nonce BLOB NOT NULL,
    sentinel_ct BLOB NOT NULL,
    sentinel_nonce BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_name_ct BLOB NOT NULL,
    name_nonce BLOB NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    wrapped_key BLOB NOT NULL,
    wrap_nonce BLOB NOT NULL,
    nonce BLOB NOT NULL,
    thumb_wrapped_key BLOB NOT NULL,
    thumb_wrap_nonce BLOB NOT NULL,
    thumb_nonce BLOB NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_photos_uploaded
    ON photos (uploaded_at DESC, id DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def vault_initialized() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM vault_config WHERE id = 1").fetchone()
        return row is not None
