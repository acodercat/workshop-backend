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
from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMember,
    OrganizationMemberRole,
)
from app.models.user import User, UserStatus
from app.services import credits
from app.services.auth.verification import verify_register_code
from app.services.credits.rates import micro_from_credits
from config import settings

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
    # Single SELECT — only ``email`` is user-supplied here; the
    # generated ``axon_${id}`` is unique by construction (id is from
    # a sequence). The code consumption in step 2 is the transactional
    # pivot — if INSERT later races on email, we re-issue codes but no
    # rows are created.
    existing = await db.execute(select(User.id).where(User.email == email))
    if existing.first() is not None:
        raise EmailTakenError("Email already registered")

    await verify_register_code(redis, email=email, code=code)

    # Reserve the id up-front so the username can encode it. ``nextval``
    # bumps the sequence atomically; if the subsequent INSERT fails for
    # any reason the id is "burned" (Postgres sequences don't roll back),
    # which is fine — id values are dense-enough for this app's scale
    # and gaps are harmless.
    #
    # User + personal org + OWNER membership ride in a single
    # transaction. The flush sequence handles the circular FK
    # (``user.organization_id`` ↔ ``organization.owner_user_id``):
    # user goes in first with ``organization_id=NULL``, org goes in
    # with ``owner_user_id=user.id``, member row links the two, then
    # we set ``user.organization_id`` to the freshly assigned org id
    # and commit. Post-commit, the invariant "every user has exactly
    # one personal org" holds. See :class:`Organization` docstring for
    # the broader rationale (mirrors OpenAI's per-user personal-org
    # auto-create at registration).
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
        # Flush so user.id is visible to the org we're about to insert.
        # Email uniqueness is the only IntegrityError we expect here —
        # the pre-check above caught the common case; this catches the
        # racing-concurrent-INSERT case where two callers passed the
        # pre-check simultaneously.
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        logger.info("register race: email=%s", email)
        raise EmailTakenError("Email already registered") from exc

    # Org + member inserts can't trip a uniqueness constraint (org has
    # no UNIQUE on (name, owner) and the member's
    # ``UNIQUE(organization_id, user_id)`` is satisfied by definition —
    # this is the first row for this fresh pair). Any IntegrityError
    # here is a schema/data bug and should propagate as a generic 500
    # rather than be mis-labelled "email taken".
    organization = Organization(
        name="Personal",
        kind=OrganizationKind.PERSONAL,
        owner_user_id=user.id,
    )
    db.add(organization)
    await db.flush()  # populate organization.id

    db.add(OrganizationMember(
        organization_id=organization.id,
        user_id=user.id,
        role=OrganizationMemberRole.OWNER,
    ))
    user.organization_id = organization.id
    await db.commit()

    # Welcome grant — best-effort, in its own transaction. Kept
    # **outside** the identity-creation commit above on purpose:
    #
    # 1. ``apply_topup`` documents that it calls ``db.rollback()``
    #    itself on a duplicate ``idempotency_key`` (its contract for
    #    keeping the session usable post-IntegrityError). If we
    #    composed it into the register tx, that rollback would wipe
    #    the freshly created user/org/member rows too.
    # 2. Any transient failure here (DB blip, lock timeout) shouldn't
    #    invalidate a registration the user already proved with email
    #    verification — they'd just have to re-verify a new code.
    #
    # The idempotency key ``welcome-{user.id}`` is unique by
    # construction (the user.id sequence is monotonic). A duplicate
    # would only happen via a manual sequence reset that re-issued an
    # old id; treat it as already-granted and continue.
    welcome_credits = settings.credit_welcome_grant_credits
    if welcome_credits > 0:
        try:
            await credits.apply_topup(
                db,
                user_id=user.id,
                amount_micro=micro_from_credits(welcome_credits),
                idempotency_key=f"welcome-{user.id}",
                reason="welcome grant",
            )
            await db.commit()
        except credits.TopupAlreadyApplied:
            # Already granted on a prior attempt — no-op, consistent
            # with apply_topup's idempotency contract.
            pass
        except Exception:
            # User is created and can log in, just with a 0 balance
            # admin needs to top up manually. Loud log so it surfaces
            # in error-rate monitoring; nothing the user can do.
            logger.exception(
                "welcome grant failed for user %s — registration committed; "
                "manual topup required",
                user.id,
            )

    await db.refresh(user)
    logger.info(
        "user registered: id=%s username=%s email=%s organization_id=%s",
        user.id,
        user.username,
        user.email,
        user.organization_id,
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
