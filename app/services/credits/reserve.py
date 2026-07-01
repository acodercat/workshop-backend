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
    CreditReservationState,
)
from app.services.credits._internal import (
    insert_ledger_and_bump_balance,
    lock_user_balance,
)
from app.services.credits.rates import reserve_micro_credits, snapshot_rate
from config import settings

logger = logging.getLogger(__name__)

# The reservation's ``expires_at`` is the sweeper's "worker probably died"
# cutoff, so it MUST exceed the longest legitimate turn — otherwise a turn
# that genuinely runs long is indistinguishable from a dead worker and gets
# wrongly refunded mid-flight (free turn). Derive it from the same config
# knob that bounds a turn (``credit_reserve_max_turn_seconds``) so a bump to
# that ceiling can't silently invert the invariant. The multiplier is the
# safety margin above one max-length turn.
_EXPIRY_TO_MAX_TURN_RATIO = 8


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
        ModelUnavailableError: ``model_id`` has no rate-card entry
            (→ 503; see
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

    # Create the reservation first so its id can stamp the RESERVE ledger
    # row's ``reservation_id`` — every row of the turn's credit trail then
    # links back through that one column. Both writes are in the caller's
    # transaction, so a rollback drops the pair together.
    #
    # ``expires_at`` is derived (see ``_EXPIRY_TO_MAX_TURN_RATIO``); defaults
    # to 1860s × 8 ≈ 4.1h. A heartbeat-based passive cancel
    # (docs/credits-future.md) is the real fix and removes the fixed timeout.
    expiry_seconds = (
        settings.credit_reserve_max_turn_seconds * _EXPIRY_TO_MAX_TURN_RATIO
    )
    reservation = CreditReservation(
        user_id=user_id,
        session_id=session_id,
        turn_id=turn_id,
        reserved=reserved,
        rate_snapshot=dict(rate),  # JSONB stores the dict copy
        state=CreditReservationState.OPEN,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds),
    )
    db.add(reservation)
    await db.flush()  # populate reservation.id for the ledger FK + callers

    await insert_ledger_and_bump_balance(
        db,
        user_id=user_id,
        amount=-reserved,
        kind=CreditLedgerKind.RESERVE,
        reservation_id=reservation.id,
        reference={
            "session_id": str(session_id),
            "turn_id": turn_id,
            "model_id": model_id,
        },
    )

    logger.info(
        "credit reserved: user=%s session=%s turn=%s reservation=%s reserved=%s",
        user_id, session_id, turn_id, reservation.id, reserved,
    )
    return reservation
