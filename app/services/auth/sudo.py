"""The sudo gate in front of every credential change.

A signed-in user changing their password or email proves they are the
account holder with exactly one of:

- the current password, or
- a sudo code sent to the email the account already has
  (:func:`request_sudo_code`).

The code exists because an OTP-registered account has never known its
password (registration stores a random placeholder), and a gate only the
password can open would leave those accounts unable to change anything.
"""

import redis.asyncio as aioredis

from app.auth import verify_password
from app.errors import IncorrectPasswordError, VerificationChannelUnboundError
from app.models.user import User
from app.schemas.user import SudoProof
from app.services.auth.verification import send_sudo_code, verify_sudo_code


async def request_sudo_code(redis: aioredis.Redis, *, user: User, locale: str = "zh-CN") -> None:
    """Send a sudo code to the account's own email."""
    if user.email is None:
        raise VerificationChannelUnboundError("Account has no email to send a code to")
    await send_sudo_code(redis, user_id=user.id, email=user.email, locale=locale)


async def require_sudo(redis: aioredis.Redis, *, user: User, proof: SudoProof) -> None:
    """Pass, or raise the typed error for the proof that failed. A sudo
    code is consumed on success, so each one opens exactly one change."""
    if proof.verification_code is not None:
        await verify_sudo_code(redis, user_id=user.id, code=proof.verification_code)
    elif proof.current_password is None or not await verify_password(
        proof.current_password, user.password_hash
    ):
        raise IncorrectPasswordError("Current password incorrect")
