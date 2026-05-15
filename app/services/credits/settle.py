"""Close out open reservations: settle with actual cost or full-refund.

Entry points:

- :func:`settle_turn` — given a reservation id + actual token counts,
  post the two settle ledger rows and flip state. **Idempotent** —
  re-entry on an already-settled reservation is a no-op.
- :func:`refund_reservation` — full refund (zero actual cost). Used
  for crashed / cancelled / lost-worker turns.
- :func:`settle_open_for_session` — settle the single OPEN reservation
  for one session, if any. Called by the janitor when it sees a
  terminal session.
- :func:`settle_open_for_user` — lazy sweep called from user-facing
  endpoints (``GET /credits``, ``queue_user_message``) to catch any of
  this user's open reservations whose sessions are already terminal.

**Where actual usage comes from**: V2 architecture reads
``session_turn.input_tokens`` / ``output_tokens``, written by the
worker in the same fenced transaction that flips
``session_turn.status`` to a terminal value. This is durable
billing-grade storage — no race with the Redis stream's TTL/delete
semantics.

V1 used to read from the Redis stream's terminal ``agent.stopped``
event, which made settle racy against ``queue_user_message`` deleting
the stream for the next turn. V2 removes that dependency entirely.
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.models.credit_ledger import CreditLedgerKind
from app.models.credit_reservation import (
    CreditReservation,
    CreditReservationState,
)
from app.models.session import Session, SessionStatus
from app.models.session_turn import SessionTurn
from app.services.credits._internal import insert_ledger_and_bump_balance
from app.services.credits.rates import RateSnapshot, cost_micro_credits

logger = logging.getLogger(__name__)


def _is_session_terminal(status: SessionStatus | None) -> bool:
    """Session has no more turns coming — safe to settle from
    ``session_turn``. ``None`` is *not* terminal in this sense;
    deleted-session reservations are handled by their own branch."""
    return status in (
        SessionStatus.COMPLETE,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    )


async def _load_reservation_for_update(
    db: AsyncSession, reservation_id: int,
) -> CreditReservation | None:
    """Lock a reservation row by id. Returns ``None`` if missing."""
    result = await db.execute(
        select(CreditReservation)
        .where(CreditReservation.id == reservation_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _read_usage_from_session_turn(
    db: AsyncSession, turn_id: int | None,
) -> tuple[int, int] | None:
    """Pull persisted ``(input_tokens, output_tokens)`` for ``turn_id``.

    Returns ``None`` when:

    - ``turn_id`` is ``None`` (sandbox reservations have no turn —
      different settle path, not yet implemented for V1);
    - the turn row no longer exists (cascade delete);
    - the row exists but tokens are ``NULL`` (worker died before the
      fenced terminal write, or the row is from the FAILED path where
      we leave tokens NULL deliberately).

    All three cases route to refund — the user shouldn't pay for a
    turn whose cost we can't measure.
    """
    if turn_id is None:
        return None
    result = await db.execute(
        select(SessionTurn.input_tokens, SessionTurn.output_tokens)
        .where(SessionTurn.id == turn_id)
    )
    row = result.one_or_none()
    if row is None or row[0] is None or row[1] is None:
        return None
    return int(row[0]), int(row[1])


async def settle_turn(
    db: AsyncSession,
    *,
    reservation_id: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
) -> None:
    """Close ``reservation_id`` with the actual cost from worker usage.

    Posts **two** ledger rows in one transaction (under the user-row
    lock used by the internal helper):

    1. ``+reserved`` reversing the original RESERVE.
    2. ``-actual``  debiting real cost computed from rate_snapshot.

    Net balance delta: ``+reserved - actual`` (typically positive —
    the user gets the over-reserved difference back).

    Idempotent: a reservation already in a terminal state is a no-op.
    Does **not** commit — caller owns transaction lifetime.
    """
    reservation = await _load_reservation_for_update(db, reservation_id)
    if reservation is None:
        logger.warning("settle_turn: reservation %s not found", reservation_id)
        return
    if reservation.state is not CreditReservationState.OPEN:
        logger.info(
            "settle_turn: reservation %s already %s — no-op",
            reservation_id, reservation.state.value,
        )
        return

    rate: RateSnapshot = reservation.rate_snapshot  # type: ignore[assignment]
    actual = cost_micro_credits(
        rate,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
    )

    # Two paired rows. The sign + ``kind=SETTLE`` already
    # disambiguates them (positive = reservation reversal, negative =
    # actual debit), so no extra discriminator on ``reference``.
    await insert_ledger_and_bump_balance(
        db,
        user_id=reservation.user_id,
        amount=+reservation.reserved,
        kind=CreditLedgerKind.SETTLE,
        reference={"reservation_id": reservation.id},
    )
    await insert_ledger_and_bump_balance(
        db,
        user_id=reservation.user_id,
        amount=-actual,
        kind=CreditLedgerKind.SETTLE,
        reference={
            "reservation_id": reservation.id,
            "turn_id": reservation.turn_id,
            "actual": {
                "input_tokens": actual_input_tokens,
                "output_tokens": actual_output_tokens,
            },
        },
    )

    reservation.state = CreditReservationState.SETTLED
    reservation.settled_at = func.now()
    await db.flush()

    logger.info(
        "credit settled: reservation=%s user=%s reserved=%s actual=%s delta=%s",
        reservation.id, reservation.user_id, reservation.reserved,
        actual, reservation.reserved - actual,
    )


async def refund_reservation(
    db: AsyncSession,
    *,
    reservation_id: int,
    reason: dict | str,
) -> None:
    """Close ``reservation_id`` with **zero** actual cost (full refund).

    One ``+reserved`` REFUND ledger row, state → CANCELLED. Used when
    the turn failed, was cancelled, or the worker was lost mid-flight
    — the user got no value, so we don't charge.

    ``reason`` is structured (dict goes verbatim into ``reference``;
    a bare string is wrapped as ``{"cause": reason}`` for backward
    compatibility). Prefer the dict form so callers don't have to
    parse f-string-encoded fields downstream.

    Idempotent in the same way as :func:`settle_turn`.
    """
    reservation = await _load_reservation_for_update(db, reservation_id)
    if reservation is None:
        logger.warning("refund: reservation %s not found", reservation_id)
        return
    if reservation.state is not CreditReservationState.OPEN:
        logger.info(
            "refund: reservation %s already %s — no-op",
            reservation_id, reservation.state.value,
        )
        return

    reason_payload = reason if isinstance(reason, dict) else {"cause": reason}
    await insert_ledger_and_bump_balance(
        db,
        user_id=reservation.user_id,
        amount=+reservation.reserved,
        kind=CreditLedgerKind.REFUND,
        reference={"reservation_id": reservation.id, **reason_payload},
    )
    reservation.state = CreditReservationState.CANCELLED
    reservation.settled_at = func.now()
    await db.flush()

    logger.info(
        "credit refunded: reservation=%s user=%s reserved=%s reason=%s",
        reservation.id, reservation.user_id, reservation.reserved, reason_payload,
    )


async def settle_open_for_session(
    db: AsyncSession,
    session_id: UUID,
) -> None:
    """Close the OPEN reservation for ``session_id``, if any.

    Reads usage from ``session_turn`` (durable) — never touches Redis.
    Idempotent: if the reservation is already terminal, returns.
    Does **not** commit — caller owns the transaction.
    """
    result = await db.execute(
        select(CreditReservation)
        .where(
            CreditReservation.session_id == session_id,
            CreditReservation.state == CreditReservationState.OPEN,
        )
    )
    reservation = result.scalar_one_or_none()
    if reservation is None:
        return  # nothing to settle

    usage = await _read_usage_from_session_turn(db, reservation.turn_id)
    if usage is None:
        await refund_reservation(
            db,
            reservation_id=reservation.id,
            reason={"cause": "usage_missing_from_session_turn"},
        )
        return
    actual_in, actual_out = usage
    await settle_turn(
        db,
        reservation_id=reservation.id,
        actual_input_tokens=actual_in,
        actual_output_tokens=actual_out,
    )


async def settle_open_for_user(
    db: AsyncSession,
    user_id: int,
) -> None:
    """Lazy sweep: settle any of ``user_id``'s OPEN reservations whose
    session is already terminal (or deleted).

    Called from user-facing endpoints (``GET /credits``,
    ``queue_user_message``). The janitor uses a similar but
    user-agnostic scan path.

    Routing:

    - session deleted (``session_id IS NULL`` after the SET-NULL FK
      kicked in) → refund, the user got no value;
    - session non-terminal → leave open, the turn isn't finished;
    - session terminal + usage on ``session_turn`` → settle with that
      usage;
    - session terminal + ``session_turn`` usage NULL (worker died /
      FAILED path) → refund.

    Typical N here is 0 or 1; partial index keeps the lookup cheap.
    """
    rows = await db.execute(
        select(
            CreditReservation.id,
            CreditReservation.turn_id,
            CreditReservation.session_id,
            Session.status,
        )
        .join(Session, Session.id == CreditReservation.session_id, isouter=True)
        .where(
            CreditReservation.user_id == user_id,
            CreditReservation.state == CreditReservationState.OPEN,
        )
    )
    for reservation_id, turn_id, session_id, session_status in rows.all():
        if session_id is None:
            # FK was SET NULL by a session delete — nothing left to
            # measure cost against, so refund. Must run *before* the
            # terminal check: ``session_status`` is None on a deleted-
            # session row and ``None not in (...)`` is True, which
            # would otherwise route to "still running" → skip → leak.
            await refund_reservation(
                db,
                reservation_id=reservation_id,
                reason={"cause": "session_deleted"},
            )
            continue
        if not _is_session_terminal(session_status):
            continue  # turn still running — leave reservation open

        usage = await _read_usage_from_session_turn(db, turn_id)
        if usage is None:
            await refund_reservation(
                db,
                reservation_id=reservation_id,
                reason={
                    "cause": "usage_missing_from_session_turn",
                    "session_status": session_status.value,
                },
            )
            continue
        actual_in, actual_out = usage
        await settle_turn(
            db,
            reservation_id=reservation_id,
            actual_input_tokens=actual_in,
            actual_output_tokens=actual_out,
        )
