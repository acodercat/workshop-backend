"""Verification-code login (login only), by email or phone.

The non-trivial half of ``POST /auth/access-tokens-by-code`` — peek the
code, look up the identifier, and issue a JWT for an existing ``ACTIVE``
user. Registration is the separate invite-gated ``POST /users``; an
unregistered identifier here raises ``not_registered`` rather than
auto-creating. The endpoint is a thin adapter over :func:`login_by_code`.
"""

import logging
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, is_user_active
from app.errors import AccountInactiveError, NotRegisteredError
from app.models.user import User
from app.schemas.auth import LoggedInResponse
from app.services.auth.verification import Channel, verify_verification_code

logger = logging.getLogger(__name__)


async def login_by_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    channel: Channel,
    identifier: str,
    code: str,
) -> LoggedInResponse:
    """Verify the OTP and log an existing user in.

    - Existing ``ACTIVE`` user → consume the code (commit to logging in)
      and return ``LoggedInResponse`` with a fresh JWT.
    - Email not registered → ``NotRegisteredError`` (404); the frontend
      steers the user to the invite-gated register tab.
    - ``DISABLED`` / ``SUSPENDED`` → :class:`~app.errors.AccountInactiveError`.
    """
    # Peek mode — validates the code, increments attempts on mismatch
    # (brute-force stays rate-limited), but doesn't delete on success.
    await verify_verification_code(
        redis, channel=channel, identifier=identifier, code=code, consume=False
    )

    id_column = User.phone if channel == "phone" else User.email
    existing = (await db.execute(select(User).where(id_column == identifier))).scalar_one_or_none()
    if existing is None:
        raise NotRegisteredError("No account for this identifier")

    if not is_user_active(existing):
        raise AccountInactiveError("Account is not active")

    # Existing ACTIVE user: consume the code now and issue the JWT. If the
    # code vanished between the peek and now, ``verify_verification_code``
    # raises ``InvalidVerificationCodeError`` for the global handler.
    await verify_verification_code(
        redis, channel=channel, identifier=identifier, code=code, consume=True
    )

    existing.last_login_at = datetime.now(UTC)
    access_token = create_access_token(
        data={"sub": existing.username, "token_version": existing.token_version},
    )
    await db.commit()
    await db.refresh(existing)
    return LoggedInResponse(
        access_token=access_token,
        token_type="bearer",
        user=existing,
    )
