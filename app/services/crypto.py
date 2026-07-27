"""Symmetric encryption for secrets stored at rest (Connector credentials).

Fernet (AES-128-CBC + HMAC) keyed by ``settings.credential_encryption_key``.
The key is shared with the worker, which decrypts the same ciphertext at turn
time — keep the two ``.env`` values identical.
"""

from functools import lru_cache

from cryptography.fernet import Fernet

from config import settings


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    key = settings.credential_encryption_key
    if not key:
        raise RuntimeError(
            "credential_encryption_key is not set; cannot encrypt/decrypt "
            "Connector credentials. Generate one with Fernet.generate_key()."
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Returns urlsafe-base64 ciphertext."""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`."""
    return _cipher().decrypt(ciphertext.encode()).decode()
