import os

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LEN = 32
NONCE_LEN = 12
SALT_LEN = 16

# Argon2id parameters. Chosen to take ~0.5s on a modern laptop while keeping
# memory cost modest enough for small VMs.
KDF_TIME_COST = 3
KDF_MEMORY_COST = 64 * 1024  # KiB → 64 MiB
KDF_PARALLELISM = 4

SENTINEL = b"lock-vault-sentinel-v1"


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def new_nonce() -> bytes:
    return os.urandom(NONCE_LEN)


def derive_key(password: str, salt: bytes) -> bytes:
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=KDF_TIME_COST,
        memory_cost=KDF_MEMORY_COST,
        parallelism=KDF_PARALLELISM,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def encrypt(key: bytes, plaintext: bytes, aad: bytes | None = None) -> tuple[bytes, bytes]:
    nonce = new_nonce()
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce, ct


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes | None = None) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def wrap_data_key(master_key: bytes) -> tuple[bytes, bytes, bytes]:
    """Generate a fresh data key and return (data_key, wrap_nonce, wrapped_key)."""
    data_key = os.urandom(KEY_LEN)
    wrap_nonce, wrapped = encrypt(master_key, data_key)
    return data_key, wrap_nonce, wrapped


def unwrap_data_key(master_key: bytes, wrap_nonce: bytes, wrapped: bytes) -> bytes:
    return decrypt(master_key, wrap_nonce, wrapped)
