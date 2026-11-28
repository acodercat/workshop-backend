"""Sign-in and sign-out orchestration: what happens in which order."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import (
    AccountInactiveError,
    IdentityProviderUnavailableError,
    InvalidCredentialsError,
)
from app.integrations.meeting.transport import MeetingSession
from app.models.user import UserStatus
from app.services.identity import credentials, signin
from app.services.identity.provider import ExternalIdentity, ProviderGrant

_GRANT = ProviderGrant(
    identity=ExternalIdentity(
        provider="meeting",
        issuer="https://meeting.test",
        subject="42",
        email="a@example.com",
        display_name="A",
    ),
    credential=MeetingSession("p", "d"),
)


def _user(status=UserStatus.ACTIVE):
    return SimpleNamespace(id=7, username="axon_7", status=status, last_login_at=None)


async def test_a_refused_sign_in_touches_nothing_local() -> None:
    db = AsyncMock()
    provider = MagicMock(authenticate=AsyncMock(side_effect=InvalidCredentialsError("no")))

    with pytest.raises(InvalidCredentialsError):
        await signin.sign_in(db, provider, account="a", password="b")

    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_a_disabled_account_gets_no_session(monkeypatch) -> None:
    monkeypatch.setattr(
        signin,
        "resolve_user",
        AsyncMock(return_value=(_user(UserStatus.DISABLED), SimpleNamespace(id=1), False)),
    )
    opened = AsyncMock()
    monkeypatch.setattr(signin, "open_session", opened)
    db = AsyncMock()
    provider = MagicMock(authenticate=AsyncMock(return_value=_GRANT), revoke=AsyncMock())

    with pytest.raises(AccountInactiveError):
        await signin.sign_in(db, provider, account="a", password="b")

    opened.assert_not_awaited()
    db.commit.assert_not_awaited()
    # The session Meeting opened for this attempt is not left open there.
    provider.revoke.assert_awaited_once_with(_GRANT.credential)


@pytest.mark.parametrize("created", [True, False])
async def test_only_a_new_account_is_welcomed(monkeypatch, created) -> None:
    monkeypatch.setattr(
        signin,
        "resolve_user",
        AsyncMock(return_value=(_user(), SimpleNamespace(id=1), created)),
    )
    monkeypatch.setattr(
        signin, "open_session", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    welcome = AsyncMock()
    monkeypatch.setattr(signin, "grant_welcome_credits", welcome)
    provider = MagicMock(authenticate=AsyncMock(return_value=_GRANT))

    result = await signin.sign_in(AsyncMock(), provider, account="a", password="b")

    assert result.access_token
    assert welcome.await_count == (1 if created else 0)


async def test_sign_out_revokes_locally_before_asking_the_provider(monkeypatch) -> None:
    order: list[str] = []
    db = AsyncMock()
    db.commit.side_effect = lambda: order.append("commit")
    sealed = credentials.seal(MeetingSession("p", "d"))

    async def revoke_row(_db, _sid):
        order.append("revoke-row")
        return sealed

    provider = MagicMock(revoke=AsyncMock(side_effect=lambda _credential: order.append("provider")))
    monkeypatch.setattr(signin, "revoke_session", revoke_row)

    await signin.sign_out(db, provider, session_id=uuid.uuid4())

    assert order == ["revoke-row", "commit", "provider"]


async def test_sign_out_survives_an_unreachable_provider(monkeypatch) -> None:
    sealed = credentials.seal(MeetingSession("p", "d"))
    monkeypatch.setattr(signin, "revoke_session", AsyncMock(return_value=sealed))
    provider = MagicMock(revoke=AsyncMock(side_effect=IdentityProviderUnavailableError("down")))

    await signin.sign_out(AsyncMock(), provider, session_id=uuid.uuid4())


async def test_signing_out_twice_does_not_call_the_provider_again(monkeypatch) -> None:
    monkeypatch.setattr(signin, "revoke_session", AsyncMock(return_value=None))
    provider = MagicMock(revoke=AsyncMock())

    await signin.sign_out(AsyncMock(), provider, session_id=uuid.uuid4())

    provider.revoke.assert_not_awaited()
