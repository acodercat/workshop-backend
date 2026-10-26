"""Registration answers "already registered" only to whoever holds the code.

Before the code was checked first, ``POST /users`` with any code at all said
``email_taken`` for a registered address and ``invalid_invite_code`` otherwise —
an account-existence oracle the login flow is careful not to be.
"""

from unittest.mock import AsyncMock

import pytest

from app.errors import InvalidVerificationCodeError
from app.services.auth import register


async def test_a_wrong_code_is_refused_before_the_identifier_is_looked_up(monkeypatch):
    verify = AsyncMock(side_effect=InvalidVerificationCodeError("Incorrect verification code"))
    monkeypatch.setattr(register, "verify_verification_code", verify)
    db = AsyncMock()

    with pytest.raises(InvalidVerificationCodeError):
        await register.register_user(
            db,
            AsyncMock(),
            identifier="a@example.com",
            code="000000",
            invite_code="X",
            name="a",
        )

    db.execute.assert_not_called()
    # Peeked, not consumed: a valid code survives a later refusal (a bad
    # invite) so the user does not have to request another.
    assert verify.await_args.kwargs["consume"] is False
