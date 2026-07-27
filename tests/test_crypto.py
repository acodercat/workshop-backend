"""Symmetric encryption for Connector credentials at rest."""

import pytest
from cryptography.fernet import Fernet

from app.services import crypto


def test_encrypt_decrypt_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto.settings, "credential_encryption_key", key)
    crypto._cipher.cache_clear()
    try:
        ciphertext = crypto.encrypt_secret("s3cr3t-token")
        assert ciphertext != "s3cr3t-token"
        assert crypto.decrypt_secret(ciphertext) == "s3cr3t-token"
    finally:
        crypto._cipher.cache_clear()


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(crypto.settings, "credential_encryption_key", "")
    crypto._cipher.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="not set"):
            crypto.encrypt_secret("x")
    finally:
        crypto._cipher.cache_clear()
