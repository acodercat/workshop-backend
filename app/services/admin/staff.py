"""Staff-account lifecycle — the console's own operator directory.

The console manages its own accounts, so unlike every other admin slice
this one has no pre-existing domain service to delegate to; the rules
live here. What earns a service module (rather than inline queries in the
endpoint, the convention elsewhere) is that the mutations carry
invariants the endpoint has no business re-deriving:

- a password change signs that account out everywhere;
- **the console never loses its last ``ACTIVE`` account** — a full
  lockout, recoverable only via ``scripts/manage_staff.py`` with
  database credentials.

Reads are not here. Listing and lookup are ordinary queries with no
invariant to protect, so they stay inline in the endpoint — the same
convention the customer-facing endpoints follow.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_password_hash, revoke_staff_tokens
from app.errors import LastActiveStaffError, StaffUsernameTakenError
from app.models.staff import Staff, StaffStatus

logger = logging.getLogger(__name__)

MIN_PASSWORD_CHARS = 12
"""Minimum length for an operator password.

Console accounts are hand-provisioned for a small, known team and guard
the highest-privilege surface in the system, so the floor is well above
the customer-facing one. Shared with the request schemas and
``scripts/manage_staff.py`` so all three agree by construction.
"""


async def create(
    db: AsyncSession, *, username: str, name: str, password: str
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
        name=name,
        password_hash=await get_password_hash(password),
    )
    db.add(account)
    await db.flush()
    return account


async def update(
    db: AsyncSession,
    account: Staff,
    *,
    name: str | None = None,
    status: StaffStatus | None = None,
) -> None:
    """Apply a partial update. ``None`` means "leave alone".

    A status change is guarded **after** the flush, so the question asked
    is the one that matters: given what this transaction is about to
    commit, does an ACTIVE account remain? A name change can't lock
    anyone out, so it skips the guard.
    """
    if name is not None:
        account.name = name
    if status is not None:
        account.status = status

    await db.flush()
    if status is not None:
        await _assert_active_staff_remains(db)


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


async def _assert_active_staff_remains(db: AsyncSession) -> None:
    """Refuse a transaction that leaves no ``ACTIVE`` account.

    The active rows are locked ``FOR UPDATE`` rather than merely counted,
    so two operators disabling each other concurrently serialize instead
    of both observing the other as the survivor. Cheap: the console has a
    handful of accounts, and this runs only on a status change.
    """
    active_ids = (
        await db.scalars(
            select(Staff.id)
            .where(Staff.status == StaffStatus.ACTIVE)
            .with_for_update()
        )
    ).all()
    if not active_ids:
        raise LastActiveStaffError(
            "This change would leave the console with no active staff "
            "account. Add or re-enable another account first."
        )
