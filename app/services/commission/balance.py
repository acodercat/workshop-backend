"""Read the commission wallet balance for a user.

Three numbers, all in RMB cents (分):

- ``total_fen``       — the cached ``user.commission_balance_fen`` (net
  of every ledger row).
- ``frozen_fen``      — commission still inside its settlement hold
  (``COMMISSION`` rows with ``available_at > now``), not yet
  withdrawable.
- ``withdrawable_fen``— ``total − frozen``: what the user can withdraw or
  convert right now.
"""

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commission_ledger import CommissionLedger, CommissionLedgerKind
from app.models.user import User
from app.services.commission._internal import lock_user_commission_balance


class CommissionBalance(NamedTuple):
    """A user's commission wallet snapshot, in RMB cents (分)."""

    total_fen: int
    frozen_fen: int
    withdrawable_fen: int


async def _frozen_fen(db: AsyncSession, user_id: int) -> int:
    """Sum the user's still-frozen ``COMMISSION`` rows (分), via the
    ``ix_commission_ledger_user_unmatured`` partial index."""
    frozen_fen = await db.scalar(
        select(func.coalesce(func.sum(CommissionLedger.amount_fen), 0)).where(
            CommissionLedger.user_id == user_id,
            CommissionLedger.kind == CommissionLedgerKind.COMMISSION,
            CommissionLedger.available_at > datetime.now(UTC),
        )
    )
    return int(frozen_fen or 0)


async def lock_withdrawable_fen(db: AsyncSession, user_id: int) -> int:
    """Withdrawable balance (分) under the user's row lock.

    Locks the user row first (so the total can't change before a debit
    lands — the gate against two concurrent withdrawals / conversions
    both passing on the full balance), then subtracts the still-frozen
    commission. Callers (withdraw / convert) write under the same lock.
    """
    total_fen = await lock_user_commission_balance(db, user_id)
    return total_fen - await _frozen_fen(db, user_id)


async def get_balance(db: AsyncSession, user_id: int) -> CommissionBalance:
    """Return ``user_id``'s commission balance split into total / frozen /
    withdrawable.

    Reads the cached total directly; sums the still-frozen ``COMMISSION``
    rows via the ``ix_commission_ledger_user_unmatured`` partial index.
    Raises ``ValueError`` if the user doesn't exist.
    """
    total_fen = await db.scalar(select(User.commission_balance_fen).where(User.id == user_id))
    if total_fen is None:
        raise ValueError(f"User {user_id} not found")

    frozen_fen = await _frozen_fen(db, user_id)
    return CommissionBalance(
        total_fen=total_fen,
        frozen_fen=frozen_fen,
        withdrawable_fen=total_fen - frozen_fen,
    )
