"""Email-change orchestration.

Two-step flow that mirrors registration but operates on an *already
authenticated* user:

1. :func:`request_email_change_code` — sudo gate (verify
   ``current_password``), uniqueness pre-check on the new address,
   send a 6-digit code to the new address.
2. :func:`change_email` — verify the code, atomically update
   ``user.email``, and bump ``token_version`` so all other sessions
   are forced to re-authenticate. Email is a password-recovery
   vector, so changing it should invalidate stale tokens the same
   way changing the password does.

Lives next to :mod:`.register` and reuses
:mod:`.verification` for the Redis code lifecycle (different
namespace, same TTL / cooldown / max-attempts settings).
"""

import logging

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import revoke_user_tokens, verify_password
from app.errors import (
    EmailTakenError,
    IncorrectPasswordError,
    SameEmailError,
)
from app.models.user import User
from app.services.auth.verification import (
    request_email_change_code as send_email_change_code,
    verify_email_change_code,
)
from app.services.email.templates import Locale

logger = logging.getLogger(__name__)


async def request_email_change_code(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    new_email: str,
    current_password: str,
    locale: Locale = "zh-CN",
) -> None:
    """Step 1: sudo + uniqueness check + dispatch the verification
    code to ``new_email``.

    Unlike registration (where revealing "this email is registered"
    would leak account existence to anonymous callers), the caller
    here is already authenticated, so returning ``email_taken`` is
    expected UX rather than an information leak.

    The ``current_password`` check happens before the uniqueness
    check on purpose — refusing on bad password before doing any
    DB lookup gives an attacker who steals a session no extra signal.
    """
    if not await verify_password(current_password, user.password_hash):
        raise IncorrectPasswordError("Current password is incorrect")

    # Refuse no-op changes — there's nothing to verify and the user
    # would just burn a code for no reason.
    if user.email is not None and user.email.lower() == new_email.lower():
        raise SameEmailError("New email is the same as the current email")

    # Uniqueness pre-check. ``UNIQUE`` partial index on ``user.email``
    # would catch a race at INSERT time too, but doing it here lets
    # us return a clear reason before burning a code.
    existing = await db.execute(
        select(User.id).where(User.email == new_email)
    )
    if existing.first() is not None:
        raise EmailTakenError("Email is already in use")

    # ``send_email_change_code`` raises typed ``AppError`` subclasses
    # (``CooldownError`` / ``EmailSendFailedError``) directly — they
    # bubble up to the endpoint handler unchanged.
    await send_email_change_code(redis, email=new_email, locale=locale)


async def change_email(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user: User,
    new_email: str,
    code: str,
) -> User:
    """Step 2: verify the code (consuming it on success), atomically
    update ``user.email``, and revoke all existing JWTs.

    Token revocation is the security pivot — even though the caller
    just proved possession of the new email, we don't trust that the
    *current* session belongs to the legitimate user (could be a
    hijacker who triggered the change). Bumping ``token_version``
    forces every active session, including this one, to re-login at
    the next request.
    """
    # ``verify_email_change_code`` raises typed ``AppError`` subclasses
    # directly (``InvalidVerificationCodeError`` /
    # ``TooManyVerificationAttemptsError``); they bubble up to the
    # endpoint handler.
    await verify_email_change_code(redis, email=new_email, code=code)

    user.email = new_email
    revoke_user_tokens(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Race: someone else claimed this email between the
        # uniqueness pre-check (step 1) and now. Surface as
        # ``email_taken`` so the user can pick another address.
        await db.rollback()
        logger.info(
            "email_change race: user_id=%s new_email=%s",
            user.id,
            new_email,
        )
        raise EmailTakenError("Email is already in use") from exc

    await db.refresh(user)
    logger.info(
        "email changed: user_id=%s new_email=%s",
        user.id,
        new_email,
    )
    return user
