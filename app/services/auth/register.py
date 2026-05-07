"""Register orchestration.

Validates the email verification code (via
:func:`app.services.auth.verification.verify_register_code`), then
INSERTs the new user row with ``status=PENDING``. Any uniqueness
collision (``username`` or ``email`` already taken) surfaces as
:class:`RegisterError`.

Lives in its own module so the endpoint stays thin and so the
verification + insert path can be unit-tested without spinning up a
full FastAPI app.
"""

import logging
from typing import Final

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_password_hash
from app.models.user import User, UserStatus
from app.services.auth.verification import VerificationError, verify_register_code

logger = logging.getLogger(__name__)


class RegisterError(Exception):
    """Raised for register-time failures the caller should map to an
    HTTP error. ``reason`` is the machine-readable kind
    (``"username_taken"`` / ``"email_taken"`` / ``"verification_*"``);
    ``message`` is human-readable."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


_USERNAME_TAKEN: Final = "username_taken"
_EMAIL_TAKEN: Final = "email_taken"


async def register_user(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    email: str,
    code: str,
    username: str,
    name: str,
    password: str,
) -> User:
    """Create a new user row.

    Steps in order — fail-fast cheap checks first, expensive ones
    last so a user picking a taken username doesn't burn a code:

    1. Pre-check ``username`` and ``email`` uniqueness.
    2. Verify (and consume) the email verification code.
    3. Hash password + INSERT (status=PENDING). Catches a race-
       induced IntegrityError and turns it back into a clear 409.
    """
    # ---- pre-check uniqueness ---------------------------------------
    # Two SELECTs: cheap, gives clean error messages before we burn
    # the verification code. The code consumption in step 2 is the
    # transactional pivot — if INSERT later races, we re-issue codes
    # but no rows are created.
    existing = await db.execute(
        select(User.id, User.username, User.email).where(
            (User.username == username) | (User.email == email)
        )
    )
    for _id, existing_username, existing_email in existing.all():
        if existing_username == username:
            raise RegisterError(_USERNAME_TAKEN, "用户名已被占用")
        if existing_email == email:
            raise RegisterError(_EMAIL_TAKEN, "该邮箱已被注册")

    # ---- verify + consume code --------------------------------------
    try:
        await verify_register_code(redis, email=email, code=code)
    except VerificationError as exc:
        # Surface verification reasons as register reasons so the
        # endpoint sees a single error type.
        raise RegisterError(
            reason=f"verification_{exc.reason}",
            message=exc.message,
        ) from exc

    # ---- create user ------------------------------------------------
    user = User(
        username=username,
        name=name,
        email=email,
        password_hash=await get_password_hash(password),
        status=UserStatus.PENDING,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Race window between pre-check and INSERT. Re-derive which
        # column collided from psycopg's ``diag.constraint_name``
        # (matches the unique index names defined in migration
        # ``12dc6d69a9ad``). Reading the structured field rather than
        # substring-matching the message text means future Postgres /
        # driver upgrades can change message wording without breaking
        # the recovery branch.
        await db.rollback()
        constraint = _violated_constraint_name(exc)
        if constraint == "ix_user_username":
            raise RegisterError(_USERNAME_TAKEN, "用户名已被占用") from exc
        if constraint == "ix_user_email":
            raise RegisterError(_EMAIL_TAKEN, "该邮箱已被注册") from exc
        # Unknown unique-violation — let the global exception handler
        # turn this into a 500 with full traceback in the logs.
        logger.error(
            "Unexpected IntegrityError on user INSERT: constraint=%r orig=%s",
            constraint,
            exc.orig,
        )
        raise

    await db.refresh(user)
    logger.info(
        "user registered: id=%s username=%s email=%s",
        user.id,
        user.username,
        user.email,
    )
    return user


def _violated_constraint_name(exc: IntegrityError) -> str | None:
    """Pull the unique-constraint name out of an ``IntegrityError``.

    psycopg surfaces ``diag.constraint_name`` on its native exception
    (which SQLAlchemy wraps as ``exc.orig``). Drivers that don't
    implement ``diag`` return ``None`` and the caller falls through to
    the catch-all path."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None
