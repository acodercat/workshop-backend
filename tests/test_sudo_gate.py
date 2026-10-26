"""The sudo gate in front of credential changes: password or sudo code.

An OTP-registered account has never known its password (registration stores
a random placeholder), so a gate only the password opens left every such
user unable to change their password or email.
"""

from unittest.mock import AsyncMock

import bcrypt
import pytest
from pydantic import ValidationError

from app.errors import (
    IncorrectPasswordError,
    InvalidVerificationCodeError,
    VerificationChannelUnboundError,
)
from app.models.user import User
from app.schemas.user import PasswordChange, SudoProof
from app.services.auth import sudo


def _user(**over) -> User:
    base = dict(
        id=7,
        username="axon_7",
        name="a",
        password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt(rounds=4)).decode(),
        email="a@example.com",
    )
    base.update(over)
    return User(**base)


class TestExactlyOneProof:
    def test_neither_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SudoProof()

    def test_both_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            SudoProof(current_password="secret", verification_code="123456")

    def test_a_code_alone_opens_a_password_change(self) -> None:
        PasswordChange(verification_code="123456", new_password="new-secret")


class TestRequireSudo:
    async def test_a_code_is_verified_against_the_users_own_key(self, monkeypatch) -> None:
        verify = AsyncMock()
        monkeypatch.setattr(sudo, "verify_sudo_code", verify)

        await sudo.require_sudo(
            AsyncMock(), user=_user(), proof=SudoProof(verification_code="123456")
        )

        verify.assert_awaited_once()
        assert verify.await_args.kwargs == {"user_id": 7, "code": "123456"}

    async def test_a_wrong_code_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(
            sudo,
            "verify_sudo_code",
            AsyncMock(side_effect=InvalidVerificationCodeError("Incorrect verification code")),
        )

        with pytest.raises(InvalidVerificationCodeError):
            await sudo.require_sudo(
                AsyncMock(), user=_user(), proof=SudoProof(verification_code="000000")
            )

    async def test_the_password_still_works(self) -> None:
        await sudo.require_sudo(
            AsyncMock(), user=_user(), proof=SudoProof(current_password="secret")
        )

    async def test_a_wrong_password_is_refused(self) -> None:
        with pytest.raises(IncorrectPasswordError):
            await sudo.require_sudo(
                AsyncMock(), user=_user(), proof=SudoProof(current_password="wrong")
            )


class TestRequestSudoCode:
    async def test_the_code_goes_to_the_accounts_own_address(self, monkeypatch) -> None:
        send = AsyncMock()
        monkeypatch.setattr(sudo, "send_sudo_code", send)

        await sudo.request_sudo_code(AsyncMock(), user=_user())

        assert send.await_args.kwargs["email"] == "a@example.com"
        assert send.await_args.kwargs["user_id"] == 7

    async def test_an_account_without_an_email_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(sudo, "send_sudo_code", AsyncMock())

        with pytest.raises(VerificationChannelUnboundError):
            await sudo.request_sudo_code(AsyncMock(), user=_user(email=None))
