"""Changing a signed-in user's email or phone — one flow, two channels.

1. :func:`request_change_code` — the sudo gate
   (:func:`~app.services.auth.sudo.require_sudo`), a refusal of a no-op
   change, a uniqueness pre-check on the new value, and a 6-digit code sent
   to it (DirectMail or SMS by channel).
2. :func:`change` — verify that code, swap the column, and bump
   ``token_version``. Email and phone are both login and recovery vectors,
   so changing either invalidates stale tokens the way a password change
   does.

The caller is already authenticated, so answering ``email_taken`` /
``phone_taken`` is expected UX rather than an account-existence leak.
"""

import logging

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import revoke_user_tokens
from app.errors import EmailTakenError, PhoneTakenError, SameEmailError, SamePhoneError
from app.models.user import User
from app.schemas.user import SudoProof
from app.services.auth.sudo import require_sudo
from app.services.auth.verification import Channel, send_change_code, verify_change_code

logger = logging.getLogger(__name__)

_TAKEN = {"email": EmailTakenError, "phone": PhoneTakenError}
_SAME = {"email": SameEmailError, "phone": SamePhoneError}


async def request_change_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    channel: Channel,
    new_value: str,
    proof: SudoProof,
    locale: str = "zh-CN",
) -> None:
    """Step 1: sudo, then send a code to *new_value*.

    The sudo check runs before any lookup, so a stolen session learns
    nothing from a bad proof. A no-op change is refused rather than burning
    a code on nothing to verify.
    """
    await require_sudo(redis, user=user, proof=proof)
    if getattr(user, channel) == new_value:
        raise _SAME[channel](f"New {channel} matches the current one")
    # The partial UNIQUE index catches a race at commit too; asking first
    # gives a clear reason before a code is spent.
    column = User.phone if channel == "phone" else User.email
    if (await db.execute(select(User.id).where(column == new_value))).first() is not None:
        raise _TAKEN[channel](f"{channel.capitalize()} already in use")
    await send_change_code(
        redis, user_id=user.id, channel=channel, identifier=new_value, locale=locale
    )


async def change(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    channel: Channel,
    new_value: str,
    code: str,
) -> User:
    """Step 2: verify the code (consuming it), swap the column, revoke JWTs.

    Revocation is the security pivot: the caller just proved they hold the
    new address, not that the *current* session is the legitimate user's.
    Bumping ``token_version`` makes every session, this one included, sign
    in again.
    """
    await verify_change_code(
        redis, user_id=user.id, channel=channel, identifier=new_value, code=code
    )
    setattr(user, channel, new_value)
    revoke_user_tokens(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Someone claimed the value between step 1's pre-check and now.
        await db.rollback()
        logger.info("%s change race: user_id=%s value=%s", channel, user.id, new_value)
        raise _TAKEN[channel](f"{channel.capitalize()} already in use") from exc

    await db.refresh(user)
    logger.info("%s changed: user_id=%s value=%s", channel, user.id, new_value)
    return user
