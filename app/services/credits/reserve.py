"""Reserve credits at the start of a turn.

Single entry: :func:`reserve_for_turn`. Sequence per call:

1. Lock the user row (``SELECT ... FOR UPDATE``).
2. Snapshot the rate-card for the model.
3. Compute ``reserve_micro`` from the rate snapshot + caller's
   ``max_input_tokens`` (pessimistic upper bound).
4. If balance < reserve_micro: raise
   :class:`~app.errors.InsufficientCreditsError`.
5. Otherwise: write the ``RESERVE`` ledger row, debit the cached
   balance, insert a ``credit_reservation`` row with the
   rate_snapshot frozen on it.

All in one transaction (caller's). On any exception, the entire
reservation rolls back — no half-applied state.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import InsufficientCreditsError
from app.models.credit_ledger import CreditLedgerKind
from app.models.credit_reservation import (
    CreditReservation,
    CreditReservationKind,
    CreditReservationState,
)
from app.services.credits._internal import (
    insert_ledger_and_bump_balance,
    lock_user_balance,
)
from app.services.credits.rates import reserve_micro_credits, snapshot_rate
from config import settings

logger = logging.getLogger(__name__)


async def reserve_for_turn(
    db: AsyncSession,
    *,
    user_id: int,
    session_id: UUID,
    turn_id: int,
    model_id: str,
) -> CreditReservation:
    """Reserve credits for one agent turn. Returns the open reservation.

    ``max_input_tokens`` for the estimate comes from
    :attr:`config.Settings.credit_reserve_max_input_tokens` — a single
    knob that biases pessimism. Settling later multi-refunds the
    unused portion based on actual usage, so over-reserving doesn't
    permanently overcharge.

    Raises:
        InsufficientCreditsError: balance below the estimated max
            cost. The HTTP layer renders this as 402 via the typed-
            error handler.
        KeyError: ``model_id`` has no rate-card entry (see
            :func:`~app.services.credits.rates.snapshot_rate`).
    """
    balance = await lock_user_balance(db, user_id)
    rate = snapshot_rate(model_id)
    reserved = reserve_micro_credits(
        rate, max_input_tokens=settings.credit_reserve_max_input_tokens,
    )
    if balance < reserved:
        # The exception message is dev-facing only; the wire payload
        # surfaces ``error.type='insufficient_credits'`` and the
        # frontend renders localized copy.
        raise InsufficientCreditsError(
            f"Insufficient credits: balance={balance} reserved={reserved}"
        )

    await insert_ledger_and_bump_balance(
        db,
        user_id=user_id,
        amount=-reserved,
        kind=CreditLedgerKind.RESERVE,
        reference={
            "session_id": str(session_id),
            "turn_id": turn_id,
            "model_id": model_id,
        },
    )
    # ``expires_at`` is the janitor's "worker probably died" cutoff
    # (``credits_janitor`` refunds OPEN reservations past it whose
    # session is still non-terminal). It MUST exceed the longest
    # legitimate turn — a deep-research / long autonomous loop that
    # genuinely runs past this window is indistinguishable from a
    # dead worker and would be wrongly refunded mid-flight (the user
    # then gets that turn for free). 4h is the conservative upper
    # bound for current workloads; a heartbeat-based passive cancel
    # (see docs/credits-future.md) is the real fix and removes the
    # need for any fixed timeout.
    reservation = CreditReservation(
        user_id=user_id,
        kind=CreditReservationKind.TURN,
        session_id=session_id,
        turn_id=turn_id,
        reserved=reserved,
        rate_snapshot=dict(rate),  # JSONB stores the dict copy
        state=CreditReservationState.OPEN,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    db.add(reservation)
    await db.flush()  # populate reservation.id for downstream callers

    logger.info(
        "credit reserved: user=%s session=%s turn=%s reservation=%s reserved=%s",
        user_id, session_id, turn_id, reservation.id, reserved,
    )
    return reservation
