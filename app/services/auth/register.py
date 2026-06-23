"""Register orchestration.

Invite-gated email-OTP registration: the caller supplies ``email`` +
``code`` + ``name`` + ``invite_code``. A valid invite is the access
grant, so this module creates the user ``ACTIVE`` and issues a JWT —
the endpoint logs them straight in. It generates a stable
``axon_${id}`` username and a random password placeholder (the user
can set a real password later in settings), mints the user's personal
referral code, and records the invite redemption — all in one
transaction.

Lives in its own module so the endpoint stays thin and so the
verification + insert path can be unit-tested without spinning up a
full FastAPI app.
"""

import logging
import secrets
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, get_password_hash
from app.errors import EmailTakenError
from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMember,
    OrganizationMemberRole,
)
from app.models.user import User, UserStatus
from app.schemas.auth import LoggedInResponse
from app.services import credits
from app.services.auth.invite import (
    claim_invite,
    create_referral_code,
    record_redemption,
)
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
    invite_code: str,
    name: str,
) -> LoggedInResponse:
    """Redeem an invite code, create an ``ACTIVE`` user, log them in.

    Steps in order — fail-fast cheap checks first so a bad invite or a
    taken email doesn't burn the verification code:

    1. Pre-check ``email`` uniqueness.
    2. Hash the password placeholder (the one slow, CPU-bound step) —
       done **before** the invite claim so the claim's row lock isn't
       held across bcrypt.
    3. Claim an invite slot (fenced ``UPDATE``) — **before** consuming
       the OTP, so a code exhausted by a concurrent registration is
       rejected without burning the verification code. The increment
       rolls back with the transaction if a later step fails.
    4. Consume the email verification code.
    5. Reserve the next user id; INSERT the user ``ACTIVE`` (the invite
       is the access grant) with the precomputed password hash.
    6. Create the personal org + OWNER membership, the user's personal
       referral code, and the invite redemption — all in the same
       transaction, so a later failure rolls the whole identity back.
    7. Issue a JWT and return the logged-in response.
    """
    # Cheap pre-check — only ``email`` is user-supplied here; the
    # generated ``axon_${id}`` is unique by construction (id is from a
    # sequence). Fail fast before the expensive steps below.
    existing = await db.execute(select(User.id).where(User.email == email))
    if existing.first() is not None:
        raise EmailTakenError("Email already registered")

    # bcrypt up-front (~250ms, CPU-bound) so no invite-code row lock is
    # held across it.
    placeholder_secret = secrets.token_urlsafe(32)
    password_hash = await get_password_hash(placeholder_secret)

    # Claim a slot BEFORE burning the OTP. The fenced UPDATE is the
    # authoritative gate; claiming first means an exhausted/expired code
    # never strands a verified-but-unused OTP. ``referrer_id`` is the
    # code owner (NULL for a SYSTEM code), used for the reward below.
    invite_id, referrer_id = await claim_invite(db, code=invite_code)

    # Irreversible OTP burn (Redis delete) — the one non-transactional
    # step. Done after the invite claim so a bad invite never consumes
    # the code; a DB failure after this rolls back the row (and the
    # claim) but not the OTP, so the user just re-requests a code.
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
    user = User(
        id=user_id,
        username=username,
        name=name,
        email=email,
        password_hash=password_hash,
        status=UserStatus.ACTIVE,
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

    # The user's personal referral code + the invite redemption audit
    # row ride in the same transaction as the identity above. The slot
    # was already fence-claimed (``claim_invite``) before the OTP burn;
    # here we just record who redeemed it.
    await create_referral_code(db, user_id=user.id)
    await record_redemption(
        db,
        invite_code_id=invite_id,
        invitee_user_id=user.id,
        referrer_user_id=referrer_id,
    )

    # A valid invite is the access grant, so log them straight in.
    user.last_login_at = datetime.now(timezone.utc)
    access_token = create_access_token(
        data={"sub": user.username, "token_version": user.token_version},
    )
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
    # The idempotency key ``welcome-{user_id}`` is unique by
    # construction (the user.id sequence is monotonic). A duplicate
    # would only happen via a manual sequence reset that re-issued an
    # old id; treat it as already-granted and continue.
    #
    # These post-commit grant blocks use the ``user_id`` local rather
    # than ``user.id``: a best-effort grant may ``db.rollback()`` on
    # failure (expiring the ORM instance), and the local can't be
    # invalidated by that.
    welcome_credits = settings.credit_welcome_grant_credits
    if welcome_credits > 0:
        try:
            await credits.apply_topup(
                db,
                user_id=user_id,
                amount_micro=micro_from_credits(welcome_credits),
                idempotency_key=f"welcome-{user_id}",
                reason="welcome grant",
            )
            await db.commit()
        except credits.TopupAlreadyApplied:
            # Already granted on a prior attempt — no-op, consistent
            # with apply_topup's idempotency contract.
            pass
        except Exception:
            # User is created and can log in, just with a 0 balance
            # admin needs to top up manually. Roll back the aborted
            # grant tx so the session stays usable for the referral
            # reward + the final ``db.refresh(user)``; the committed
            # registration is untouched. Loud log so it surfaces in
            # error-rate monitoring; nothing the user can do.
            await db.rollback()
            logger.exception(
                "welcome grant failed for user %s — registration committed; "
                "manual topup required",
                user_id,
            )

    # Referral reward — credit the referrer for a successful registration
    # via their personal code. Best-effort, in its own transaction, for
    # the same reasons as the welcome grant above: ``apply_referral_reward``
    # self-rolls-back on a duplicate idempotency key, which must not undo
    # the committed registration. SYSTEM codes have no referrer, so this
    # only fires for REFERRAL codes. The ``referral-{invitee}`` key is
    # unique by construction (the invitee id is fresh).
    reward_credits = settings.invite_referral_reward_credits
    if referrer_id is not None and reward_credits > 0:
        try:
            await credits.apply_referral_reward(
                db,
                referrer_user_id=referrer_id,
                invitee_user_id=user_id,
                amount_micro=micro_from_credits(reward_credits),
                idempotency_key=f"referral-{user_id}",
            )
            await db.commit()
        except credits.ReferralRewardAlreadyApplied:
            pass
        except Exception:
            # The invitee is registered and logged in; only the
            # referrer's bonus failed. Roll back the aborted grant tx so
            # the session stays usable for the final ``db.refresh(user)``;
            # the committed registration is untouched. Loud log; nothing
            # the invitee can do, and an admin can grant it manually.
            await db.rollback()
            logger.exception(
                "referral reward failed: referrer=%s invitee=%s — "
                "registration committed",
                referrer_id, user_id,
            )

    await db.refresh(user)
    logger.info(
        "user registered: id=%s username=%s email=%s organization_id=%s",
        user.id,
        user.username,
        user.email,
        user.organization_id,
    )
    return LoggedInResponse(
        access_token=access_token,
        token_type="bearer",
        user=user,
    )


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
