"""Staff-account lifecycle — the console's own operator directory.

The console manages its own accounts, so unlike every other admin slice
this one has no pre-existing domain service to delegate to; the rules
live here. What earns a service module (rather than inline queries in the
endpoint, the convention elsewhere) is that each mutation carries an
invariant the endpoint has no business re-deriving:

- a password change signs that account out everywhere;
- **the console never loses its last active ``OWNER``**.

That second rule is why :func:`update` takes the whole partial change
rather than exposing one setter per column: a single PATCH can demote
*and* disable, and the invariant is about the state the transaction ends
in, not about either field on its own.

Reads are not here. Listing and lookup are ordinary queries with no
invariant to protect, so they stay inline in the endpoint — the same
convention the customer-facing endpoints follow.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_password_hash, revoke_staff_tokens
from app.errors import LastOwnerError, StaffUsernameTakenError
from app.models.staff import Staff, StaffRole, StaffStatus

logger = logging.getLogger(__name__)

MIN_PASSWORD_CHARS = 12
"""Minimum length for an operator password.

Console accounts are hand-provisioned for a small, known team and guard
the highest-privilege surface in the system, so the floor is well above
the customer-facing one. Shared with the request schemas and
``scripts/manage_staff.py`` so all three agree by construction.
"""


async def create(
    db: AsyncSession,
    *,
    username: str,
    display_name: str,
    role: StaffRole,
    password: str,
) -> Staff:
    """Create an operator account and flush it, so the caller has its id.

    Does **not** commit — the caller's :func:`~app.services.admin.audit`
    block does, together with the audit row.

    The username pre-check is for the typed 409; the table's UNIQUE
    constraint is what actually guarantees it, and a lost race there
    surfaces through the global ``IntegrityError`` handler.
    """
    if await db.scalar(select(Staff.id).where(Staff.username == username)):
        raise StaffUsernameTakenError(f"Staff username {username!r} is taken.")

    account = Staff(
        username=username,
        display_name=display_name,
        role=role,
        password_hash=await get_password_hash(password),
    )
    db.add(account)
    await db.flush()
    return account


async def update(
    db: AsyncSession,
    account: Staff,
    *,
    display_name: str | None = None,
    role: StaffRole | None = None,
    status: StaffStatus | None = None,
) -> None:
    """Apply a partial update, refusing one that would lock everyone out.

    ``None`` means "leave alone" — an absent field, not a value to write.

    The last-``OWNER`` check runs **after** the change is flushed, so it
    asks the only question that matters: given everything this
    transaction is about to commit, does an active ``OWNER`` remain? A
    pre-flight check on each field separately would pass a PATCH that
    demotes the last owner and disables the replacement in one call.
    """
    if display_name is not None:
        account.display_name = display_name
    if role is not None:
        account.role = role
    if status is not None:
        account.status = status

    await db.flush()
    await _assert_active_owner_remains(db)


async def set_password(db: AsyncSession, account: Staff, password: str) -> None:
    """Set a new password and sign the account out everywhere.

    Revocation is not optional here: the reason to change a console
    password is a suspected leak or a departure, and leaving the old
    tokens live would defeat both. Callers who merely want to sign an
    account out bump :func:`~app.auth.revoke_staff_tokens` directly.
    """
    account.password_hash = await get_password_hash(password)
    revoke_staff_tokens(account)
    await db.flush()


async def _assert_active_owner_remains(db: AsyncSession) -> None:
    """Refuse a transaction that leaves no active ``OWNER``.

    The owner rows are locked ``FOR UPDATE`` rather than merely counted,
    so two operators demoting each other concurrently serialize instead
    of both observing the other as the survivor. Cheap: the console has a
    handful of accounts, and this runs only on a staff mutation.
    """
    owner_ids = (
        await db.scalars(
            select(Staff.id)
            .where(Staff.role == StaffRole.OWNER, Staff.status == StaffStatus.ACTIVE)
            .with_for_update()
        )
    ).all()
    if not owner_ids:
        raise LastOwnerError(
            "This change would leave the console with no active OWNER. "
            "Promote a replacement first."
        )
