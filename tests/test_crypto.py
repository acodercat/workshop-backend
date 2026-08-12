"""Symmetric encryption for Connector credentials at rest."""

import pytest
from cryptography.fernet import Fernet

from app.errors import ConnectorCredentialUnreadableError
from app.services import crypto


def test_encrypt_decrypt_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto.settings, "mcp_credential_encryption_key", key)
    crypto._cipher.cache_clear()
    try:
        ciphertext = crypto.encrypt_secret("s3cr3t-token")
        assert ciphertext != "s3cr3t-token"
        assert crypto.decrypt_secret(ciphertext) == "s3cr3t-token"
    finally:
        crypto._cipher.cache_clear()


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(crypto.settings, "mcp_credential_encryption_key", "")
    crypto._cipher.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="not set"):
            crypto.encrypt_secret("x")
    finally:
        crypto._cipher.cache_clear()


class TestOpeningASecretThatWillNotOpen:
    """A stored secret outlives the key that sealed it. Rotate the key, or damage
    the column, and every path that needs it fails — the question is only whether
    the user is told something they can act on."""

    def test_a_rotated_key_is_a_named_failure(self, monkeypatch):
        monkeypatch.setattr(
            crypto.settings,
            "mcp_credential_encryption_key",
            Fernet.generate_key().decode(),
        )
        crypto._cipher.cache_clear()
        sealed = crypto.encrypt_secret("s3cr3t-token")

        monkeypatch.setattr(
            crypto.settings,
            "mcp_credential_encryption_key",
            Fernet.generate_key().decode(),
        )
        crypto._cipher.cache_clear()

        with pytest.raises(ConnectorCredentialUnreadableError) as caught:
            crypto.open_secret(sealed, what="credential")

        # 409, not the 500 a raw ``InvalidToken`` reaches the client as: the
        # request was fine, the row is not, and re-entering the secret fixes it.
        assert caught.value.status_code == 409
        assert "credential" in str(caught.value)
        # Never the value, and never Fernet's own words about it.
        assert sealed not in str(caught.value)
        crypto._cipher.cache_clear()
