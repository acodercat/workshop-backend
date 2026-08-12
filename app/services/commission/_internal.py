"""Private write primitives. The only file in the codebase that
``INSERT``s into ``commission_ledger`` or ``UPDATE``s
``user.commission_balance_fen``.

The cash-side twin of :mod:`app.services.credits._internal` — same
shape, same row-lock + flush discipline, different table/column and
the **RMB-cents** unit. Every commission-moving operation (accrual,
withdraw, convert, clawback, adjust) funnels into
:func:`insert_ledger_and_bump_balance`.

Why concentrate writes here:

- The invariant ``user.commission_balance_fen ==
  SUM(commission_ledger.amount_fen)`` is enforced at the service
  boundary, not the DB layer. If every caller inlined its own INSERT +
  UPDATE the invariant would break the first time someone forgot the
  matching update.
- ``FOR UPDATE`` on the user row serialises concurrent writes for the
  *same user* — two parallel withdrawals pass through one at a time,
  the second sees the first's debit. Different users don't contend.
- Auditors only need to read one file to know how commission moves.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commission_ledger import CommissionLedger, CommissionLedgerKind
from app.models.user import User

logger = logging.getLogger(__name__)


async def lock_user_commission_balance(db: AsyncSession, user_id: int) -> int:
    """Lock the user row and return the current commission balance (分).

    Holds the row lock until the surrounding transaction commits or
    rolls back. Callers that read the balance for a decision (withdraw /
    convert) must also write under the same lock — that's what
    ``FOR UPDATE`` enforces.
    """
    result = await db.execute(
        select(User.commission_balance_fen).where(User.id == user_id).with_for_update()
    )
    balance = result.scalar_one_or_none()
    if balance is None:
        raise ValueError(f"User {user_id} not found")
    return balance


async def insert_ledger_and_bump_balance(
    db: AsyncSession,
    *,
    user_id: int,
    amount_fen: int,
    kind: CommissionLedgerKind,
    available_at: datetime | None = None,
    reference: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> CommissionLedger:
    """Atomic ledger INSERT + balance bump under user-row lock.

    Does **not** commit — the caller owns transaction lifetime. This
    lets callers compose multiple ledger writes into one commit.

    Re-entry safety on duplicate ``idempotency_key`` is the caller's
    responsibility (catch ``IntegrityError`` and treat as already-done).
    The UNIQUE constraint on the column is what makes that safe.

    ``amount_fen`` is signed RMB cents; sign matches the operation
    (``COMMISSION`` / ``WITHDRAW_REFUND`` are positive; ``WITHDRAW`` /
    ``CONVERT`` / ``CLAWBACK`` are negative; ``ADJUST`` is either).
    ``available_at`` is set only on ``COMMISSION`` rows (the settlement
    hold); NULL elsewhere.
    """
    # FOR UPDATE on the user row before reading or writing balance — see
    # module docstring. We re-issue the lock here even when callers have
    # already locked, because Postgres treats re-locking as a no-op.
    await lock_user_commission_balance(db, user_id)

    entry = CommissionLedger(
        user_id=user_id,
        amount_fen=amount_fen,
        kind=kind,
        available_at=available_at,
        reference=reference or {},
        idempotency_key=idempotency_key,
    )
    db.add(entry)

    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(commission_balance_fen=User.commission_balance_fen + amount_fen)
    )
    await db.flush()  # surface IntegrityError on duplicate idempotency_key now

    logger.info(
        "commission ledger: user=%s kind=%s amount_fen=%s ref=%s",
        user_id,
        kind.value,
        amount_fen,
        reference,
    )
    return entry
