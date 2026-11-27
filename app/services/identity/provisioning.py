"""Creating the local account for an identity seen for the first time.

Split from :mod:`.federation` because the two answer different questions —
that module decides *whether* an account must be created, this one knows
*what an account is*: a ``user`` row, its personal organization, and the
OWNER membership tying them together. The invariant "every user has
exactly one personal org" is established here and nowhere else.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMember,
    OrganizationMemberRole,
)
from app.models.user import User, UserStatus
from app.services import credits
from app.services.credits.rates import micro_from_credits
from config import settings

logger = logging.getLogger(__name__)

# ``user.name`` is ``String(255)``; a provider's display name is not ours
# to bound, so it is cut to fit rather than allowed to fail the sign-in.
_MAX_NAME_LENGTH = 255


async def provision_user(db: AsyncSession, *, name: str, email: str | None) -> User:
    """Insert a new ``ACTIVE`` user with its personal organization.

    Flushes, never commits — the caller links the identity in the same
    transaction, so an account can never exist without the identity that
    is the only way into it.

    Being vouched for by the identity provider is the access grant, which
    is why the row is ``ACTIVE`` from the start and carries no password.
    """
    # Reserve the id up-front so the username can encode it. ``nextval``
    # is not rolled back if the transaction is, so an abandoned attempt
    # burns an id; gaps are harmless.
    user_id = await _reserve_next_user_id(db)
    user = User(
        id=user_id,
        username=f"axon_{user_id}",
        name=name[:_MAX_NAME_LENGTH],
        email=email,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    # The flush order walks the circular FK (``user.organization_id`` ↔
    # ``organization.owner_user_id``): the user goes in with no org, the
    # org goes in owned by the user, then the user is pointed at it.
    await db.flush()

    organization = Organization(
        name="Personal",
        kind=OrganizationKind.PERSONAL,
        owner_user_id=user.id,
    )
    db.add(organization)
    await db.flush()

    db.add(
        OrganizationMember(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationMemberRole.OWNER,
        )
    )
    user.organization_id = organization.id
    await db.flush()
    return user


async def grant_welcome_credits(db: AsyncSession, *, user_id: int) -> None:
    """Credit a newly provisioned account. Best-effort, in its own transaction.

    Call **after** the provisioning transaction has committed, never inside
    it: ``apply_topup`` rolls the session back itself on a duplicate
    ``idempotency_key``, which composed into the provisioning transaction
    would take the new account with it. And a failure here is no reason
    to refuse a sign-in the provider has already approved — the user gets
    in with a zero balance and an operator tops them up.

    ``welcome-{user_id}`` makes a retry a no-op, so being called twice for
    one account (two first sign-ins racing) grants once.
    """
    welcome_credits = settings.credit_welcome_grant_credits
    if welcome_credits <= 0:
        return
    try:
        await credits.apply_topup(
            db,
            user_id=user_id,
            amount_micro=micro_from_credits(welcome_credits),
            idempotency_key=f"welcome-{user_id}",
            reason="welcome grant",
            source="welcome_grant",
        )
        await db.commit()
    except credits.TopupAlreadyApplied:
        pass
    except Exception:
        # Leave the session usable for whatever the caller does next.
        await db.rollback()
        logger.exception(
            "welcome grant failed for user %s — account created; manual topup required",
            user_id,
        )


async def _reserve_next_user_id(db: AsyncSession) -> int:
    """Allocate the next ``user.id`` from its serial sequence.

    ``pg_get_serial_sequence`` resolves the sequence from the column's
    metadata, so this survives the sequence being renamed.
    """
    result = await db.execute(text("SELECT nextval(pg_get_serial_sequence('user', 'id'))"))
    return int(result.scalar_one())
