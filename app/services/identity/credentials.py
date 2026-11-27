"""Sealing the user's Meeting session for the ``identity_session`` row.

The credential is the user's live session at Meeting — whoever
reads one can act as them there. So it is encrypted before it touches the
database, under a key only the backend holds
(``settings.identity_credential_encryption_key``; deliberately not the
Connector key, which the worker shares).

Fernet, as in :mod:`app.services.crypto`: authenticated, so a tampered or
truncated value fails to open instead of opening as garbage.
"""

import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.integrations.meeting.transport import MeetingSession
from config import settings


class CredentialUnreadable(Exception):
    """A stored credential would not open — the key changed, or the value
    is damaged. Permanent for that row: the session layer treats it like a
    revoked session, since the user signing in again is the only fix."""


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    return Fernet(settings.identity_credential_encryption_key.encode())


def seal(session: MeetingSession) -> str:
    """Encrypt a session for storage."""
    value = {"passport": session.passport, "device_id": session.device_id}
    return _cipher().encrypt(json.dumps(value).encode()).decode()


def unseal(sealed: str) -> MeetingSession:
    """Decrypt a value produced by :func:`seal`."""
    try:
        value = json.loads(_cipher().decrypt(sealed.encode()))
        return MeetingSession(passport=value["passport"], device_id=value["device_id"])
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise CredentialUnreadable from exc
