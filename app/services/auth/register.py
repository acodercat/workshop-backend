"""Register orchestration.

Validates the email verification code (via
:func:`app.services.auth.verification.verify_register_code`), then
INSERTs the new user row. The email-OTP unified flow is the only
supported entry today: caller supplies just ``email`` + ``code`` +
``name``, this module generates a stable ``axon_${id}`` username and
a random password placeholder (the user can set a real password
later in settings if they want to use the username + password
sign-in path).

Lives in its own module so the endpoint stays thin and so the
verification + insert path can be unit-tested without spinning up a
full FastAPI app.
"""

import logging
import secrets

import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_password_hash
from app.errors import EmailTakenError
from app.models.user import User, UserStatus
from app.services.auth.verification import verify_register_code

logger = logging.getLogger(__name__)


async def register_user(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    email: str,
    code: str,
    name: str,
) -> User:
    """Create a new user via the email-OTP flow.

    Steps in order — fail-fast cheap checks first, expensive ones
    last so a user with a taken email doesn't burn a code:

    1. Pre-check ``email`` uniqueness.
    2. Verify (and consume) the email verification code.
    3. Reserve the next user id from the sequence so the auto-
       generated ``axon_${id}`` username is known *before* INSERT.
    4. INSERT user with ``status=PENDING``. The email is verified
       (proof of address), but access is gated on a separate admin
       approval step — login endpoints surface ``account_pending``
       and refuse to issue a JWT until an admin flips the status to
       ``ACTIVE``. Random password hash acts as a placeholder; the
       ``POST /auth/access-tokens`` form-grant login can't
       authenticate them with this hash anyway, which is correct:
       new accounts are email-OTP only until the user sets a real
       password in settings.
    """
    # ---- pre-check uniqueness ---------------------------------------
    # Single SELECT — only ``email`` is user-supplied here; the
    # generated ``axon_${id}`` is unique by construction (id is from
    # a sequence). The code consumption in step 2 is the transactional
    # pivot — if INSERT later races on email, we re-issue codes but no
    # rows are created.
    existing = await db.execute(select(User.id).where(User.email == email))
    if existing.first() is not None:
        raise EmailTakenError("Email already registered")

    # ---- verify + consume code --------------------------------------
    await verify_register_code(redis, email=email, code=code)

    # ---- create user ------------------------------------------------
    # Reserve the id up-front so the username can encode it. ``nextval``
    # bumps the sequence atomically; if the subsequent INSERT fails for
    # any reason the id is "burned" (Postgres sequences don't roll back),
    # which is fine — id values are dense-enough for this app's scale
    # and gaps are harmless.
    user_id = await _reserve_next_user_id(db)
    username = f"axon_{user_id}"
    placeholder_secret = secrets.token_urlsafe(32)
    user = User(
        id=user_id,
        username=username,
        name=name,
        email=email,
        password_hash=await get_password_hash(placeholder_secret),
        status=UserStatus.PENDING,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Race window between the pre-check and INSERT on email — the
        # only user-supplied unique field. ``ix_user_username`` could
        # only fire if two registrations raced into the same sequence
        # value, which the sequence itself prevents; treat it as a 500
        # if it ever happens.
        await db.rollback()
        constraint = _violated_constraint_name(exc)
        if constraint == "ix_user_email":
            raise EmailTakenError("Email already registered") from exc
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


async def _reserve_next_user_id(db: AsyncSession) -> int:
    """Allocate the next ``user.id`` value from its serial sequence.

    Used so the auto-generated ``axon_${id}`` username can be set on
    the same INSERT as everything else, instead of doing a flush +
    UPDATE roundtrip with a placeholder username. ``pg_get_serial_
    sequence`` resolves the sequence name from the ``id`` column's
    metadata, so this stays correct even if the sequence is renamed
    in a future migration.
    """
    result = await db.execute(
        text("SELECT nextval(pg_get_serial_sequence('user', 'id'))")
    )
    return int(result.scalar_one())


def _violated_constraint_name(exc: IntegrityError) -> str | None:
    """Pull the unique-constraint name out of an ``IntegrityError``.

    psycopg surfaces ``diag.constraint_name`` on its native exception
    (which SQLAlchemy wraps as ``exc.orig``). Drivers that don't
    implement ``diag`` return ``None`` and the caller falls through to
    the catch-all path."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None
