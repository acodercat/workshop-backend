"""Manual admin correction (``ADJUST`` ledger rows).

Different from :mod:`app.services.credits.topup` because:

- Signed amount — supports both grants and clawbacks.
- ``reference.reason`` is mandatory and audited; ops should describe
  *why* the correction is being made (ticket id, incident, etc.).

Same idempotency contract as :mod:`.topup`.
"""

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_ledger import CreditLedger, CreditLedgerKind
from app.services.credits._internal import insert_ledger_and_bump_balance

logger = logging.getLogger(__name__)


class AdjustmentAlreadyApplied(Exception):
    """Raised when an adjust with the same ``idempotency_key`` exists."""


async def apply_adjust(
    db: AsyncSession,
    *,
    user_id: int,
    amount_micro: int,
    idempotency_key: str,
    reason: str,
    admin_id: int | None = None,
) -> CreditLedger:
    """Insert a signed ``ADJUST`` row + bump balance.

    Does **not** commit on success — caller owns transaction lifetime.
    Same self-rollback-on-duplicate behaviour as
    :func:`app.services.credits.topup.apply_topup`; see that
    docstring for the rationale.

    ``amount_micro = 0`` is rejected (would be a no-op row
    that only adds audit noise). Use a real correction or no row at
    all.
    """
    if amount_micro == 0:
        raise ValueError("Adjustment amount must be non-zero")

    try:
        return await insert_ledger_and_bump_balance(
            db,
            user_id=user_id,
            amount=amount_micro,
            kind=CreditLedgerKind.ADJUST,
            reference={"reason": reason, "admin_id": admin_id},
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        await db.rollback()
        logger.info(
            "adjust duplicate idempotency_key=%s user_id=%s",
            idempotency_key, user_id,
        )
        raise AdjustmentAlreadyApplied(
            f"Adjustment with idempotency_key={idempotency_key!r} already applied"
        ) from exc
