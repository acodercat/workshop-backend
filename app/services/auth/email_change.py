"""Changing a signed-in user's email.

1. :func:`request_email_change_code` — the sudo gate
   (:func:`~app.services.auth.sudo.require_sudo`), a refusal of a no-op
   change, a uniqueness pre-check on the new address, and a 6-digit code
   sent to it.
2. :func:`change_email` — verify that code, swap the column, and bump
   ``token_version``. Email is a login and recovery vector, so changing
   it invalidates stale tokens the way a password change does.

The caller is already authenticated, so answering ``email_taken`` is
expected UX rather than an account-existence leak.
"""

import logging

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import revoke_user_tokens
from app.errors import EmailTakenError, SameEmailError
from app.models.user import User
from app.schemas.user import SudoProof
from app.services.auth.sudo import require_sudo
from app.services.auth.verification import (
    send_email_change_code,
    verify_email_change_code,
)

logger = logging.getLogger(__name__)


async def request_email_change_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    new_email: str,
    proof: SudoProof,
    locale: str = "zh-CN",
) -> None:
    """Step 1: sudo, then send a code to *new_email*.

    The sudo check runs before any lookup, so a stolen session learns
    nothing from a bad proof. A no-op change is refused rather than burning
    a code on nothing to verify.
    """
    await require_sudo(redis, user=user, proof=proof)
    if user.email is not None and user.email.lower() == new_email.lower():
        raise SameEmailError("New email matches the current one")
    # The ``user_email_key`` constraint catches a race at commit too; asking
    # first gives a clear reason before a code is spent.
    if (await db.execute(select(User.id).where(User.email == new_email))).first() is not None:
        raise EmailTakenError("Email already in use")
    await send_email_change_code(redis, user_id=user.id, email=new_email, locale=locale)


async def change_email(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    new_email: str,
    code: str,
) -> User:
    """Step 2: verify the code (consuming it), swap the column, revoke JWTs.

    Revocation is the security pivot: the caller just proved they hold the
    new address, not that the *current* session is the legitimate user's.
    Bumping ``token_version`` makes every session, this one included, sign
    in again.
    """
    await verify_email_change_code(redis, user_id=user.id, email=new_email, code=code)
    user.email = new_email
    revoke_user_tokens(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Someone claimed the address between step 1's pre-check and now.
        await db.rollback()
        logger.info("email change race: user_id=%s new_email=%s", user.id, new_email)
        raise EmailTakenError("Email already in use") from exc

    await db.refresh(user)
    logger.info("email changed: user_id=%s new_email=%s", user.id, new_email)
    return user
