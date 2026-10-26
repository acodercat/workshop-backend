"""Credit top-ups — the only entry point that creates ``TOPUP`` ledger rows.

Every grant of credits someone paid for or was given comes through here:
the welcome grant, and an operator's grant (``scripts/topup_credits``).
``source`` says which, on the row.
"""

import logging
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_ledger import CreditLedger, CreditLedgerKind
from app.services.credits._internal import insert_ledger_and_bump_balance

logger = logging.getLogger(__name__)

TopupSource = Literal["welcome_grant", "admin"]


class TopupAlreadyApplied(Exception):
    """Raised when a top-up with the same ``idempotency_key`` exists.

    Callers treat this as success — the prior call already moved the
    balance, so re-trying is a no-op."""


async def apply_topup(
    db: AsyncSession,
    *,
    user_id: int,
    amount_micro: int,
    idempotency_key: str,
    reason: str,
    source: TopupSource,
    admin_id: int | None = None,
) -> CreditLedger:
    """Insert a ``TOPUP`` row + bump balance, atomic under user lock.

    Does **not** commit on success — caller (script, endpoint) owns
    the transaction so the top-up can compose with other work.

    **Exception**: on duplicate ``idempotency_key`` the implementation
    rolls the session back itself before raising
    :class:`TopupAlreadyApplied`. Postgres marks the session
    transaction as aborted on the UNIQUE violation, so the caller
    cannot continue without a rollback; doing it here lets the caller
    resume cleanly (e.g. read the prior balance to confirm the earlier
    success). Treat the rollback as part of this function's contract
    when composing with other writes.

    ``amount_micro`` must be positive — a negative top-up is a
    different operation (call :func:`app.services.credits.adjust.apply_adjust`).
    """
    if amount_micro <= 0:
        raise ValueError("Top-up amount must be positive; use apply_adjust for corrections")

    try:
        return await insert_ledger_and_bump_balance(
            db,
            user_id=user_id,
            amount=amount_micro,
            kind=CreditLedgerKind.TOPUP,
            reference={"reason": reason, "admin_id": admin_id, "source": source},
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        # UNIQUE violation on idempotency_key — the previous call
        # already succeeded. Roll back this tx but treat as success
        # at the caller boundary.
        await db.rollback()
        logger.info(
            "topup duplicate idempotency_key=%s user_id=%s (treated as success)",
            idempotency_key,
            user_id,
        )
        raise TopupAlreadyApplied(
            f"Top-up with idempotency_key={idempotency_key!r} already applied"
        ) from exc
