"""Changing a signed-in user's email: sudo first, then a code to the new address."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import EmailTakenError, IncorrectPasswordError, SameEmailError
from app.models.user import User
from app.schemas.user import SudoProof
from app.services.auth import email_change

_PROOF = SudoProof(current_password="secret")


def _user() -> User:
    return User(id=7, username="axon_7", name="a", email="a@example.com")


def _db(*, taken: bool) -> AsyncMock:
    db = AsyncMock()
    db.execute.return_value = MagicMock(first=MagicMock(return_value=(8,) if taken else None))
    return db


async def _request(db: AsyncMock, new_email: str) -> None:
    await email_change.request_email_change_code(
        db, AsyncMock(), user=_user(), new_email=new_email, proof=_PROOF
    )


async def test_a_refused_proof_stops_before_any_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        email_change,
        "require_sudo",
        AsyncMock(side_effect=IncorrectPasswordError("Current password incorrect")),
    )
    send = AsyncMock()
    monkeypatch.setattr(email_change, "send_email_change_code", send)
    db = _db(taken=True)

    with pytest.raises(IncorrectPasswordError):
        await _request(db, "b@example.com")

    db.execute.assert_not_called()
    send.assert_not_called()


async def test_a_no_op_change_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(email_change, "require_sudo", AsyncMock())

    with pytest.raises(SameEmailError):
        await _request(_db(taken=False), "A@example.com")


async def test_an_address_someone_else_holds_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(email_change, "require_sudo", AsyncMock())

    with pytest.raises(EmailTakenError):
        await _request(_db(taken=True), "b@example.com")


async def test_the_code_goes_to_the_new_address(monkeypatch) -> None:
    monkeypatch.setattr(email_change, "require_sudo", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(email_change, "send_email_change_code", send)

    await _request(_db(taken=False), "b@example.com")

    assert send.await_args.kwargs == {"user_id": 7, "email": "b@example.com", "locale": "zh-CN"}


async def test_a_change_swaps_the_column_and_signs_everyone_out(monkeypatch) -> None:
    monkeypatch.setattr(email_change, "verify_email_change_code", AsyncMock())
    user = _user()
    user.token_version = 3

    await email_change.change_email(
        AsyncMock(), AsyncMock(), user=user, new_email="b@example.com", code="123456"
    )

    assert user.email == "b@example.com"
    assert user.token_version == 4
