"""Email-OTP login: verify the code, then branch login-vs-register.

The non-trivial half of ``POST /auth/access-tokens-by-code`` — peek the
code, look up the email, and either issue a JWT for an existing
``ACTIVE`` user or signal that the frontend should collect a name. The
endpoint is a thin adapter over :func:`login_by_code`.
"""

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import assert_user_active, create_access_token
from app.models.user import User
from app.schemas.auth import EmailLoginExisting, EmailLoginNewUser
from app.services.auth.verification import verify_register_code

logger = logging.getLogger(__name__)


async def login_by_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    email: str,
    code: str,
) -> EmailLoginExisting | EmailLoginNewUser:
    """Verify the OTP and either log the user in or signal registration.

    Branches after a peek-verify (which does NOT consume the code):

    - Existing ``ACTIVE`` user → consume the code (commit to logging in)
      and return ``EmailLoginExisting`` with a fresh JWT.
    - Email not registered → return ``EmailLoginNewUser`` and leave the
      code untouched; ``POST /users`` is the consume-pivot that turns the
      verified code into a row.
    - ``PENDING`` → ``AccountPendingError`` (403); ``DISABLED`` /
      ``SUSPENDED`` → opaque 401. Both via :func:`app.auth.assert_user_active`.

    Peeking (not consuming) for the new-user case is deliberate: the user
    may take a while typing their name, and consuming here would strand a
    verified intent with no way to finish without a fresh code.
    """
    # Peek mode — validates the code, increments attempts on mismatch
    # (brute-force stays rate-limited), but doesn't delete on success.
    await verify_register_code(redis, email=email, code=code, consume=False)

    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is None:
        return EmailLoginNewUser()

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
    return EmailLoginExisting(
        access_token=access_token,
        token_type="bearer",
        user=existing,
    )
