"""Phone-change orchestration.

The SMS-channel sibling of :mod:`.email_change`: a two-step flow on an
*already authenticated* user:

1. :func:`request_phone_change_code` — sudo gate (verify
   ``current_password``), uniqueness pre-check on the new number, send a
   6-digit code (via Aliyun SMS) to the new number.
2. :func:`change_phone` — verify the code, atomically update
   ``user.phone``, and bump ``token_version`` so all other sessions are
   forced to re-authenticate. Phone is a login + recovery vector, so
   changing it invalidates stale tokens the same way changing the
   password or email does.

Reuses :mod:`.verification` for the Redis code lifecycle (the
``phone_change`` namespace, same TTL / cooldown / max-attempts settings).
"""

import logging

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import revoke_user_tokens, verify_password
from app.errors import (
    IncorrectPasswordError,
    PhoneTakenError,
    SamePhoneError,
)
from app.models.user import User
from app.services.auth.verification import (
    send_phone_change_code,
    verify_phone_change_code,
)

logger = logging.getLogger(__name__)


async def request_phone_change_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    new_phone: str,
    current_password: str,
) -> None:
    """Step 1: sudo + uniqueness check + dispatch the verification code
    to ``new_phone``.

    The caller is already authenticated, so returning ``phone_taken``
    is expected UX rather than an account-existence leak. The
    ``current_password`` check happens before the DB lookup so a stolen
    session gets no extra signal on a bad password.
    """
    if not await verify_password(current_password, user.password_hash):
        raise IncorrectPasswordError("Current password incorrect")

    # Refuse no-op changes — nothing to verify, would just burn a code.
    if user.phone is not None and user.phone == new_phone:
        raise SamePhoneError("New phone matches current phone")

    # Uniqueness pre-check. The partial UNIQUE index on ``user.phone``
    # catches a race at commit time too, but checking here returns a
    # clear reason before burning a code.
    existing = await db.execute(select(User.id).where(User.phone == new_phone))
    if existing.first() is not None:
        raise PhoneTakenError("Phone already in use")

    await send_phone_change_code(redis, user_id=user.id, phone=new_phone)


async def change_phone(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    new_phone: str,
    code: str,
) -> User:
    """Step 2: verify the code (consuming it on success), atomically
    update ``user.phone``, and revoke all existing JWTs.

    Token revocation is the security pivot: even though the caller just
    proved possession of the new number, we don't trust that the
    *current* session is the legitimate user. Bumping ``token_version``
    forces every active session, including this one, to re-login.
    """
    await verify_phone_change_code(
        redis, user_id=user.id, phone=new_phone, code=code,
    )

    user.phone = new_phone
    revoke_user_tokens(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Race: someone else claimed this number between the uniqueness
        # pre-check (step 1) and now. Surface as ``phone_taken``.
        await db.rollback()
        logger.info(
            "phone_change race: user_id=%s new_phone=%s", user.id, new_phone
        )
        raise PhoneTakenError("Phone already in use") from exc

    await db.refresh(user)
    logger.info("phone changed: user_id=%s new_phone=%s", user.id, new_phone)
    return user
