"""Private write primitives. The only file in the codebase that
``INSERT``s into ``credit_ledger`` or ``UPDATE``s ``user.credit_balance``.

Every credit-moving operation goes through one of two helpers here:

- :func:`insert_ledger_and_bump_balance` — atomic ledger row + balance
  delta, under a row-level lock on the user. This is the workhorse;
  reserve / settle / refund / topup / adjust all funnel into it.
- :func:`lock_user_balance` — ``SELECT credit_balance FROM "user"
  WHERE id = ? FOR UPDATE``. Pulled out because reserve needs the
  balance value before deciding whether to insert.

Why concentrate writes here:

- The invariant ``user.credit_balance == SUM(credit_ledger.amount)``
  is enforced at the service boundary, not the DB layer. If every
  caller inlined their own INSERT + UPDATE the invariant would be
  violated the first time someone forgot the matching update.
- ``FOR UPDATE`` on the user row serialises concurrent writes for the
  *same user* — two parallel reserve attempts pass through one at a
  time, the second sees the first's debit. Different users don't
  contend.
- Auditors only need to read one file to know how credit moves.
"""

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_ledger import CreditLedger, CreditLedgerKind
from app.models.user import User

logger = logging.getLogger(__name__)


async def lock_user_balance(db: AsyncSession, user_id: int) -> int:
    """Lock the user row and return the current balance (µ-credits).

    Holds the row lock until the surrounding transaction commits or
    rolls back. Callers that read balance for a decision (reserve)
    must also write under the same lock — that's what ``FOR UPDATE``
    enforces.
    """
    result = await db.execute(
        select(User.credit_balance).where(User.id == user_id).with_for_update()
    )
    balance = result.scalar_one_or_none()
    if balance is None:
        raise ValueError(f"User {user_id} not found")
    return balance


async def insert_ledger_and_bump_balance(
    db: AsyncSession,
    *,
    user_id: int,
    amount: int,
    kind: CreditLedgerKind,
    reference: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    reservation_id: int | None = None,
) -> CreditLedger:
    """Atomic ledger INSERT + balance bump under user-row lock.

    Does **not** commit — the caller owns transaction lifetime. This
    lets callers compose multiple ledger writes (settle posts two
    rows) into one commit.

    Re-entry safety on duplicate ``idempotency_key`` is the caller's
    responsibility (catch ``IntegrityError`` and treat as already-done).
    The UNIQUE constraint on the column is what makes that safe.

    ``amount`` is signed µ-credits; sign matches the operation
    (RESERVE / SETTLE-debit are negative; TOPUP / SETTLE-reverse /
    REFUND are positive; ADJUST is either).

    ``reservation_id`` links a turn-related row (RESERVE / SETTLE /
    REFUND) back to its :class:`~app.models.credit_reservation.CreditReservation`
    via the typed FK column; ``None`` for rows unrelated to a hold.
    """
    # FOR UPDATE on the user row before reading or writing balance —
    # see module docstring. We re-issue the lock here even when callers
    # have already locked, because Postgres treats re-locking as a
    # no-op.
    await lock_user_balance(db, user_id)

    entry = CreditLedger(
        user_id=user_id,
        amount=amount,
        kind=kind,
        reference=reference or {},
        idempotency_key=idempotency_key,
        reservation_id=reservation_id,
    )
    db.add(entry)

    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(credit_balance=User.credit_balance + amount)
    )
    await db.flush()  # surface IntegrityError on duplicate idempotency_key now

    logger.info(
        "credit ledger: user=%s kind=%s amount=%s ref=%s",
        user_id, kind.value, amount, reference,
    )
    return entry
