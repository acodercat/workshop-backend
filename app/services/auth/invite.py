"""Invite-code creation + redemption.

Two code origins (see :class:`app.models.invite_code.InviteCode`):
``SYSTEM`` codes seed the cold start (created via the
``scripts.create_invite_code`` CLI), ``REFERRAL`` codes are each
user's personal code, minted at registration.

Redemption is two steps so the slot can be claimed before the
irreversible OTP burn: :func:`claim_invite` fence-claims a slot
(``used_count`` increment guarded by ``max_uses``, via an atomic
``UPDATE … RETURNING``) and :func:`record_redemption` writes the
:class:`app.models.invite_redemption.InviteRedemption` audit row once
the invitee exists. Like the credits internals, these helpers **flush
but never commit** — the caller (``register_user``) owns the
transaction so the user row and the redemption land atomically.
"""

import logging
import secrets
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.errors import (
    InvalidInviteCodeError,
    InviteExhaustedError,
    InviteExpiredError,
)
from app.models.invite_code import InviteCode, InviteCodeOrigin
from app.models.invite_redemption import InviteRedemption
from config import settings

logger = logging.getLogger(__name__)

# Short, human-typeable codes (8 chars is the mainstream sweet spot for
# referral codes). Uppercase alphanumerics minus the ambiguous glyphs
# 0/O/1/I/L so a hand-typed or read-aloud code resolves unambiguously;
# lookups normalize to uppercase (:func:`_normalize_code`). 31^8 ≈ 8e11
# keeps codes unguessable at this app's scale; the UNIQUE column plus
# the collision retry below cover the birthday case.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 8
_MAX_CODE_ATTEMPTS = 5


def _generate_invite_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _normalize_code(code: str) -> str:
    """Codes are generated uppercase, so accept any-case / padded input."""
    return code.strip().upper()


async def create_invite_code(
    db: AsyncSession,
    *,
    origin: InviteCodeOrigin,
    owner_user_id: int | None = None,
    max_uses: int | None = None,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> InviteCode:
    """INSERT an invite code. Shared by the referral and SYSTEM paths.

    Retries on the (astronomically rare) ``code`` collision under a
    SAVEPOINT so the outer transaction survives. Flushes (so ``id`` /
    constraint violations surface now) but does not commit. The
    ``origin``/``owner_user_id`` consistency CHECK and the
    one-referral-per-user partial-unique index are enforced by the
    schema, not re-validated here.
    """
    last_exc: IntegrityError | None = None
    for _ in range(_MAX_CODE_ATTEMPTS):
        invite = InviteCode(
            code=_generate_invite_code(),
            origin=origin,
            owner_user_id=owner_user_id,
            max_uses=max_uses,
            expires_at=expires_at,
            note=note,
        )
        try:
            async with db.begin_nested():
                db.add(invite)
                await db.flush()
        except IntegrityError as exc:
            # Likely a ``code`` collision — the SAVEPOINT rolled back, so
            # retry with a fresh code. A persistent violation (e.g. a
            # duplicate referral owner) re-raises its real cause below.
            last_exc = exc
            continue
        return invite
    assert last_exc is not None
    raise last_exc


async def create_referral_code(db: AsyncSession, *, user_id: int) -> InviteCode:
    """Mint the user's personal referral code (one per user, enforced by
    ``uq_invite_code_referral_owner``). Capped at
    ``settings.invite_referral_max_uses``."""
    return await create_invite_code(
        db,
        origin=InviteCodeOrigin.REFERRAL,
        owner_user_id=user_id,
        max_uses=settings.invite_referral_max_uses,
    )


async def get_or_create_referral_code(db: AsyncSession, *, user_id: int) -> InviteCode:
    """Return the user's personal referral code, minting one if absent.

    New users get a code at registration; the lazy create is a backstop
    for accounts that predate the referral feature. Flushes on create,
    does not commit — the caller owns the transaction.
    """
    stmt = select(InviteCode).where(
        InviteCode.owner_user_id == user_id,
        InviteCode.origin == InviteCodeOrigin.REFERRAL,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    try:
        return await create_referral_code(db, user_id=user_id)
    except IntegrityError:
        # A concurrent first-view created it (the one-per-user
        # ``uq_invite_code_referral_owner`` index); re-read the winner.
        return (await db.execute(stmt)).scalar_one()


async def _classify_unredeemable(db: AsyncSession, *, normalized_code: str) -> NoReturn:
    """Raise the precise typed error for a code the fenced claim didn't
    match (not found / disabled / expired / exhausted). Only hit on the
    claim's failure path, so the extra SELECT costs nothing on the
    happy path."""
    invite = (
        await db.execute(select(InviteCode).where(InviteCode.code == normalized_code))
    ).scalar_one_or_none()
    if invite is None or not invite.enabled:
        raise InvalidInviteCodeError("Invite code not found")
    if invite.expires_at is not None and invite.expires_at <= datetime.now(UTC):
        raise InviteExpiredError("Invite code expired")
    if invite.max_uses is not None and invite.used_count >= invite.max_uses:
        raise InviteExhaustedError("Invite code exhausted")
    # Redeemable again now (the blocker cleared concurrently) — treat the
    # lost-race as exhausted; the caller can retry.
    raise InviteExhaustedError("Invite code exhausted")


async def claim_invite(db: AsyncSession, *, code: str) -> tuple[int, int | None]:
    """Atomically claim one redemption slot. Returns ``(invite_code_id,
    referrer_user_id)`` (``referrer`` is the code owner, NULL for a
    SYSTEM code).

    The fenced ``UPDATE ... WHERE used_count < max_uses ... RETURNING``
    is the **authoritative gate and the only read** on the happy path —
    it serializes only against concurrent redemptions of the *same*
    code. Call this **before** the irreversible OTP consume so a code
    exhausted by a concurrent registration is rejected without burning
    the caller's verification code; ``rowcount == 0`` means not
    redeemable, and :func:`_classify_unredeemable` raises the precise
    reason. Does not commit — the increment rolls back with the
    transaction if a later step fails.
    """
    normalized = _normalize_code(code)
    row = (
        await db.execute(
            update(InviteCode)
            .where(
                InviteCode.code == normalized,
                InviteCode.enabled.is_(True),
                or_(
                    InviteCode.expires_at.is_(None),
                    InviteCode.expires_at > func.now(),
                ),
                or_(
                    InviteCode.max_uses.is_(None),
                    InviteCode.used_count < InviteCode.max_uses,
                ),
            )
            .values(used_count=InviteCode.used_count + 1)
            .returning(InviteCode.id, InviteCode.owner_user_id)
        )
    ).first()
    if row is None:
        await _classify_unredeemable(db, normalized_code=normalized)
    return row.id, row.owner_user_id


async def record_redemption(
    db: AsyncSession,
    *,
    invite_code_id: int,
    invitee_user_id: int,
    referrer_user_id: int | None,
) -> InviteRedemption:
    """Write the redemption audit row for an already-claimed invite.
    Flushes, no commit."""
    redemption = InviteRedemption(
        invite_code_id=invite_code_id,
        invitee_user_id=invitee_user_id,
        referrer_user_id=referrer_user_id,
    )
    db.add(redemption)
    await db.flush()
    logger.info(
        "invite redeemed: code_id=%s invitee=%s referrer=%s",
        invite_code_id,
        invitee_user_id,
        referrer_user_id,
    )
    return redemption
