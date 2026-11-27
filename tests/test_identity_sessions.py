"""Session re-confirmation, token authentication, and credential sealing.

The database writes in :mod:`app.services.identity.sessions` go through
three small functions (claim, confirm, revoke); these tests replace them
with an in-memory row so the decisions around them can be exercised
offline.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app import auth
from app.errors import IdentityProviderUnavailableError
from app.integrations.meeting.transport import MeetingSession
from app.models.user import UserStatus
from app.services.identity import credentials, sessions
from app.services.identity.provider import (
    CredentialRejected,
    ExternalIdentity,
    ProviderGrant,
)
from config import settings

_SUBJECT = "42"


def _grant(subject: str = _SUBJECT, passport: str = "p") -> ProviderGrant:
    return ProviderGrant(
        identity=ExternalIdentity(
            provider="meeting",
            issuer="https://meeting.test",
            subject=subject,
            email=None,
            display_name="A",
        ),
        credential=MeetingSession(passport, "d"),
    )


class _Row:
    """One ``identity_session`` row, and the three writes made to it."""

    def __init__(self, monkeypatch, *, claimable: bool = True) -> None:
        self.sealed = credentials.seal(MeetingSession("p", "d"))
        self.claimable = claimable
        self.claims = 0
        self.confirmed: datetime | None = None
        self.revoked = False
        monkeypatch.setattr(sessions, "_claim", self._claim)
        monkeypatch.setattr(sessions, "_confirm", self._confirm)
        monkeypatch.setattr(sessions, "end_session", self._revoke)

    async def _claim(self, session_id, *, due_before):
        self.claims += 1
        return self.sealed if self.claimable else None

    async def _confirm(self, session_id, *, confirmed_at, sealed, rotated):
        self.confirmed = confirmed_at
        if rotated is not None and sealed == self.sealed:
            self.sealed = credentials.seal(rotated)

    async def _revoke(self, session_id):
        self.revoked = True


def _ages(*, checked: float, verified: float) -> dict:
    now = datetime.now(UTC)
    return {
        "session_id": uuid.uuid4(),
        "subject": _SUBJECT,
        "checked_at": now - timedelta(seconds=checked),
        "verified_at": now - timedelta(seconds=verified),
    }


_INTERVAL = settings.identity_reverify_interval_seconds
_GRACE = settings.identity_reverify_grace_seconds


async def test_a_recently_checked_session_does_not_call_the_provider(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock())

    await sessions.ensure_confirmed(**_ages(checked=1, verified=1), provider=provider)

    assert row.claims == 0
    provider.verify.assert_not_awaited()


async def test_a_due_session_is_reconfirmed(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock(return_value=_grant()))

    await sessions.ensure_confirmed(
        **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
    )

    assert row.confirmed is not None
    assert not row.revoked


async def test_a_rotated_credential_replaces_the_stored_one(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock(return_value=_grant(passport="new")))

    await sessions.ensure_confirmed(
        **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
    )

    assert credentials.unseal(row.sealed).passport == "new"


async def test_a_session_the_provider_rejects_is_revoked(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock(side_effect=CredentialRejected))

    with pytest.raises(sessions.SessionEnded):
        await sessions.ensure_confirmed(
            **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
        )

    assert row.revoked


async def test_a_credential_answering_for_someone_else_is_revoked(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock(return_value=_grant(subject="99")))

    with pytest.raises(sessions.SessionEnded):
        await sessions.ensure_confirmed(
            **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
        )

    assert row.revoked


async def test_an_unreadable_credential_ends_the_session(monkeypatch) -> None:
    row = _Row(monkeypatch)
    row.sealed = "not-a-fernet-token"
    provider = MagicMock(verify=AsyncMock())

    with pytest.raises(sessions.SessionEnded):
        await sessions.ensure_confirmed(
            **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
        )

    assert row.revoked
    provider.verify.assert_not_awaited()


async def test_an_outage_within_grace_keeps_the_session(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock(side_effect=IdentityProviderUnavailableError("down")))

    await sessions.ensure_confirmed(
        **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
    )

    assert not row.revoked
    assert row.confirmed is None


async def test_an_outage_past_grace_is_a_503_not_a_sign_out(monkeypatch) -> None:
    row = _Row(monkeypatch)
    provider = MagicMock(verify=AsyncMock(side_effect=IdentityProviderUnavailableError("down")))

    with pytest.raises(IdentityProviderUnavailableError):
        await sessions.ensure_confirmed(
            **_ages(checked=_INTERVAL + 1, verified=_GRACE + 1), provider=provider
        )

    assert not row.revoked


async def test_a_lost_claim_proceeds_on_the_last_confirmation(monkeypatch) -> None:
    # Another request is re-verifying this session right now.
    row = _Row(monkeypatch, claimable=False)
    provider = MagicMock(verify=AsyncMock())

    await sessions.ensure_confirmed(
        **_ages(checked=_INTERVAL + 1, verified=_INTERVAL + 1), provider=provider
    )

    provider.verify.assert_not_awaited()
    assert row.claims == 1


def test_a_sealed_credential_round_trips_and_is_not_plaintext() -> None:
    sealed = credentials.seal(MeetingSession("secret-passport", "d"))

    assert "secret-passport" not in sealed
    assert credentials.unseal(sealed) == MeetingSession("secret-passport", "d")


@pytest.mark.parametrize("sealed", ["garbage", credentials._cipher().encrypt(b"[1, 2]").decode()])
def test_a_damaged_credential_is_unreadable(sealed) -> None:
    with pytest.raises(credentials.CredentialUnreadable):
        credentials.unseal(sealed)


#
# ``auth._authenticate`` — the access token is only as good as its session.


def _token(**claims) -> str:
    payload = {"sub": "axon_1", "sid": str(uuid.uuid4()), **claims}
    return auth.create_access_token({k: v for k, v in payload.items() if v is not None})


def _db(row) -> AsyncMock:
    result = MagicMock()
    result.one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    return db


def _session_row(*, revoked: bool = False, expired: bool = False, status=UserStatus.ACTIVE):
    now = datetime.now(UTC)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revoked_at=now if revoked else None,
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(days=1),
        checked_at=now,
        verified_at=now,
    )
    user = SimpleNamespace(username="axon_1", status=status)
    return (session, user, _SUBJECT)


async def test_a_live_session_authenticates(monkeypatch) -> None:
    monkeypatch.setattr(sessions, "ensure_confirmed", AsyncMock())
    row = _session_row()

    principal = await auth._authenticate(_db(row), _token(), MagicMock())

    assert principal.user is row[1]
    assert principal.session_id == row[0].id


@pytest.mark.parametrize(
    "row",
    [
        None,  # no such session
        _session_row(revoked=True),
        _session_row(expired=True),
        _session_row(status=UserStatus.DISABLED),
    ],
)
async def test_a_dead_session_is_an_opaque_401(monkeypatch, row) -> None:
    monkeypatch.setattr(sessions, "ensure_confirmed", AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await auth._authenticate(_db(row), _token(), MagicMock())

    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        # Minted before sessions existed: names no session, so honours none.
        auth.create_access_token({"sub": "axon_1"}),
        _token(sid="not-a-uuid"),
        _token(sub=None),
    ],
)
async def test_a_token_naming_no_session_is_refused_before_the_database(token) -> None:
    with pytest.raises(HTTPException) as caught:
        await auth._authenticate(None, token, None)

    assert caught.value.status_code == 401


async def test_a_token_for_another_username_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(sessions, "ensure_confirmed", AsyncMock())

    with pytest.raises(HTTPException):
        await auth._authenticate(_db(_session_row()), _token(sub="axon_2"), MagicMock())


async def test_a_session_the_provider_ended_is_a_401(monkeypatch) -> None:
    monkeypatch.setattr(sessions, "ensure_confirmed", AsyncMock(side_effect=sessions.SessionEnded))

    with pytest.raises(HTTPException) as caught:
        await auth._authenticate(_db(_session_row()), _token(), MagicMock())

    assert caught.value.status_code == 401


async def test_a_provider_outage_reaches_the_client_as_itself(monkeypatch) -> None:
    # Not swallowed into a 401: a client reads 401 as "signed out".
    monkeypatch.setattr(
        sessions,
        "ensure_confirmed",
        AsyncMock(side_effect=IdentityProviderUnavailableError("down")),
    )

    with pytest.raises(IdentityProviderUnavailableError):
        await auth._authenticate(_db(_session_row()), _token(), MagicMock())
