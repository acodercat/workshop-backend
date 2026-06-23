"""Email-OTP login (login only).

The non-trivial half of ``POST /auth/access-tokens-by-code`` — peek the
code, look up the email, and issue a JWT for an existing ``ACTIVE``
user. Registration is the separate invite-gated ``POST /users``; an
unregistered email here raises ``not_registered`` rather than
auto-creating. The endpoint is a thin adapter over :func:`login_by_code`.
"""

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import assert_user_active, create_access_token
from app.errors import NotRegisteredError
from app.models.user import User
from app.schemas.auth import LoggedInResponse
from app.services.auth.verification import verify_register_code

logger = logging.getLogger(__name__)


async def login_by_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    email: str,
    code: str,
) -> LoggedInResponse:
    """Verify the OTP and log an existing user in.

    - Existing ``ACTIVE`` user → consume the code (commit to logging in)
      and return ``LoggedInResponse`` with a fresh JWT.
    - Email not registered → ``NotRegisteredError`` (404); the frontend
      steers the user to the invite-gated register tab.
    - ``DISABLED`` / ``SUSPENDED`` → opaque 401 via
      :func:`app.auth.assert_user_active`.
    """
    # Peek mode — validates the code, increments attempts on mismatch
    # (brute-force stays rate-limited), but doesn't delete on success.
    await verify_register_code(redis, email=email, code=code, consume=False)

    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is None:
        raise NotRegisteredError("No account for this email")

    assert_user_active(existing, inactive_detail="Account is not active")

    # Existing ACTIVE user: consume the code now and issue the JWT. If the
    # code vanished between the peek and now, ``verify_register_code``
    # raises ``InvalidVerificationCodeError`` for the global handler.
    await verify_register_code(redis, email=email, code=code, consume=True)

    existing.last_login_at = datetime.now(timezone.utc)
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
