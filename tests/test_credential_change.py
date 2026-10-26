"""Changing a signed-in user's email or phone — one flow, two channels."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import EmailTakenError, PhoneTakenError, SameEmailError, SamePhoneError
from app.models.user import User
from app.schemas.user import SudoProof
from app.services.auth import credential_change

_PROOF = SudoProof(current_password="secret")


def _user() -> User:
    return User(id=7, username="axon_7", name="a", email="a@example.com", phone="13800138000")


def _db(*, taken: bool) -> AsyncMock:
    db = AsyncMock()
    db.execute.return_value = MagicMock(first=MagicMock(return_value=(8,) if taken else None))
    return db


@pytest.fixture(autouse=True)
def _sudo_passes(monkeypatch):
    monkeypatch.setattr(credential_change, "require_sudo", AsyncMock())


@pytest.mark.parametrize(
    ("channel", "current", "same_error"),
    [("email", "a@example.com", SameEmailError), ("phone", "13800138000", SamePhoneError)],
)
async def test_a_no_op_change_is_refused(channel, current, same_error) -> None:
    with pytest.raises(same_error):
        await credential_change.request_change_code(
            _db(taken=False),
            AsyncMock(),
            user=_user(),
            channel=channel,
            new_value=current,
            proof=_PROOF,
        )


@pytest.mark.parametrize(
    ("channel", "value", "taken_error"),
    [("email", "b@example.com", EmailTakenError), ("phone", "13900139000", PhoneTakenError)],
)
async def test_a_value_someone_else_holds_is_refused(channel, value, taken_error) -> None:
    with pytest.raises(taken_error):
        await credential_change.request_change_code(
            _db(taken=True),
            AsyncMock(),
            user=_user(),
            channel=channel,
            new_value=value,
            proof=_PROOF,
        )


async def test_the_code_goes_to_the_new_value_on_its_channel(monkeypatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr(credential_change, "send_change_code", send)

    await credential_change.request_change_code(
        _db(taken=False),
        AsyncMock(),
        user=_user(),
        channel="phone",
        new_value="13900139000",
        proof=_PROOF,
    )

    assert send.await_args.kwargs == {
        "user_id": 7,
        "channel": "phone",
        "identifier": "13900139000",
        "locale": "zh-CN",
    }


async def test_a_change_swaps_the_column_and_signs_everyone_out(monkeypatch) -> None:
    monkeypatch.setattr(credential_change, "verify_change_code", AsyncMock())
    user = _user()
    user.token_version = 3

    await credential_change.change(
        AsyncMock(),
        AsyncMock(),
        user=user,
        channel="email",
        new_value="b@example.com",
        code="123456",
    )

    assert user.email == "b@example.com"
    assert user.token_version == 4
