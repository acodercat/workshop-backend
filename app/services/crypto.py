"""Symmetric encryption for secrets stored at rest (Connector credentials).

Fernet (AES-128-CBC + HMAC) keyed by ``settings.mcp_credential_encryption_key``.
The key is shared with the worker, which decrypts the same ciphertext at turn
time — keep the two ``.env`` values identical.
"""

from functools import lru_cache

from cryptography.fernet import Fernet

from app.errors import ConnectorCredentialUnreadableError
from config import settings


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    key = settings.mcp_credential_encryption_key
    if not key:
        raise RuntimeError(
            "mcp_credential_encryption_key is not set; cannot encrypt/decrypt "
            "Connector credentials. Generate one with Fernet.generate_key()."
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Returns urlsafe-base64 ciphertext."""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`."""
    return _cipher().decrypt(ciphertext.encode()).decode()


def open_secret(ciphertext: str, *, what: str) -> str:
    """Decrypt one stored secret, naming the failure when it will not open.

    A Fernet failure is permanent for that row — the key was rotated, or the
    column is damaged — and no caller can do anything but tell the user to
    supply the secret again. Raised as a typed error rather than let out as
    ``InvalidToken``, which reaches the client as a 500 that says nothing. One
    spelling of the rule, here, because a caller that respells it can misspell
    it. *what* names the secret for the message; the value never appears.
    """
    try:
        return decrypt_secret(ciphertext)
    except Exception as exc:
        raise ConnectorCredentialUnreadableError(
            f"This Connector's {what} could not be read. Set it again."
        ) from exc
