"""Backend-side terminal reconciliation for stranded sessions.

Normally the **worker** is the sole writer of a terminal
``session.status`` (``COMPLETE`` / ``CANCELLED`` / ``FAILED``): the
backend only ever stamps ``IDLE`` at creation and flips ``RUNNING`` at
enqueue, and trusts a worker to close the turn out. That trust has one
hole — if the **entire worker fleet is down** (or a turn is enqueued
and never claimed), nobody writes the terminal. The session is then
pinned ``RUNNING`` forever: ``DELETE`` 409s, new messages 409, and the
cancel pub/sub reaches no one. The worker's own stale-lease recovery
cron (``expire_stale_turns``) can't help — it only runs while a worker
is alive.

:func:`fail_abandoned_turn` is the backend's last-resort reconciliation
for that case, invoked by the credit sweeper (the one background process
guaranteed to keep running) once a reservation has sat OPEN past its
``expires_at`` — the "the worker is definitively gone" signal. It flips
the stranded turn **and** its session to ``FAILED`` so the session stops
being a zombie, composed into the sweeper's refund transaction so money
and lifecycle reconcile atomically.

:func:`cancel_unclaimed_turn` is the same mechanism turned toward a
different hole: a user who presses stop while their turn is still
``PENDING`` in the queue (all worker slots busy, or the fleet
mid-deploy). The cancel endpoint's pub/sub signal only reaches a worker
that has already *claimed* the turn, so an unclaimed turn's cancel would
otherwise be dropped and the turn later claimed and billed. It flips the
``PENDING`` turn + session → ``CANCELLED`` and refunds, synchronously in
the request.

**Why this is safe against a still-alive worker.** The worker's terminal
writes are fenced on ``worker_id = ? AND status = RUNNING`` (turn) and
``status = RUNNING`` (session). A backend terminal write is therefore a
*poison pill*: once we flip the turn out of ``RUNNING``, the worker's
fenced UPDATE matches no row, rolls back, and never overwrites us.
:func:`fail_abandoned_turn` only ever flips a turn whose lease is already
dead (or absent), so it doesn't fight a worker genuinely mid-flight on a
long turn — that row is left untouched and the next sweep re-evaluates
once its lease finally lapses. :func:`cancel_unclaimed_turn` only touches
``PENDING`` turns, which by construction have no worker at all (the claim
sets ``worker_id`` + the lease atomically with the ``RUNNING`` flip), so
a concurrent claim simply wins the row-lock race and leaves the backend
UPDATE matching zero rows.
"""

import logging
import uuid

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.models.session import Session, SessionStatus
from app.models.session_turn import (
    SessionTurn,
    SessionTurnStatus,
    TerminationReason,
)
from app.services.credits import settle_open_for_session

logger = logging.getLogger(__name__)

# Turn statuses that mean "not yet closed out" — the only ones a recovery
# flip should touch. PENDING = enqueued, never claimed; RUNNING = claimed
# but the worker never wrote a terminal.
_NON_TERMINAL_TURN = (SessionTurnStatus.PENDING, SessionTurnStatus.RUNNING)


async def fail_abandoned_turn(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
) -> bool:
    """Terminalize a session whose worker never wrote a terminal status.

    Flips the session's single non-terminal turn → ``FAILED`` and, **iff**
    that flip touched a row, the parent session → ``FAILED`` — so the two
    never diverge. Runs in the caller's transaction without committing, so
    it composes atomically with the sweeper's ``refund_reservation``.

    The turn flip is gated on a dead / absent lease
    (``lease_expires_at IS NULL OR < now()``): a worker actively
    heartbeating a long turn keeps a fresh lease, so its row is excluded
    and left to finish normally — see the module docstring on why a
    backend ``FAILED`` write can't clobber a live worker.

    Returns ``True`` when the session was reconciled, ``False`` when
    nothing was done (live lease, or no non-terminal turn). The caller
    treats ``False`` as "may still produce value — don't refund yet."

    At most one non-terminal turn exists per session (enqueue 409-guards
    on ``status == RUNNING``), so the ``session_id``-scoped flip targets
    exactly the stranded turn without needing the turn id.
    """
    turn_result = await db.execute(
        update(SessionTurn)
        .where(
            SessionTurn.session_id == session_id,
            SessionTurn.status.in_(_NON_TERMINAL_TURN),
            or_(
                SessionTurn.lease_expires_at.is_(None),
                SessionTurn.lease_expires_at < func.now(),
            ),
        )
        .values(
            status=SessionTurnStatus.FAILED,
            termination_reason=TerminationReason.WORKER_LOST,
            lease_expires_at=None,
            terminated_at=func.now(),
        )
    )
    if turn_result.rowcount == 0:  # type: ignore[attr-defined]  # UPDATE returns a CursorResult
        # Either a worker grabbed the turn between sweeps and holds a live
        # lease, or there's no non-terminal turn. Don't flip the session —
        # keeping turn+session in lockstep is the whole point.
        return False

    # Mirror the worker's ``RUNNING``-guarded flip so a status moved by
    # anything else (a racing real terminal write, manual operator
    # intervention) isn't clobbered.
    await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.status == SessionStatus.RUNNING)
        .values(status=SessionStatus.FAILED)
    )
    logger.warning(
        "recovery: terminalized abandoned session %s (turn + session → FAILED)",
        session_id,
    )
    return True


async def cancel_unclaimed_turn(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
) -> bool:
    """Cancel a turn the user stopped before any worker claimed it.

    The cancel endpoint's pub/sub signal only reaches a worker that has
    already *claimed* the turn (and registered a live runner). A turn
    still ``PENDING`` in the queue — all worker slots busy, or the fleet
    mid-deploy — has nobody listening, so the signal is dropped and the
    turn would later be claimed and run to completion, billing a user who
    asked to stop. This closes that hole from the backend.

    Flips the session's ``PENDING`` turn → ``CANCELLED`` and, iff that
    touched a row, the parent session → ``CANCELLED``, then settles the
    open reservation. A never-claimed turn has no measured usage in
    ``session_turn``, so the settle helper refunds it in full. Runs in the
    caller's transaction without committing.

    Only ``PENDING`` turns are touched: such a turn is by definition
    unclaimed (the worker sets ``worker_id`` + ``lease_expires_at``
    atomically with the ``RUNNING`` flip), so there is no worker to race
    for a graceful mid-turn shutdown — a claimed/``RUNNING`` turn stays on
    the pub/sub path. The ``status = PENDING`` fence also makes this
    race-free against a concurrent claim: the two UPDATEs serialize on the
    turn row, and if the worker's claim commits first this UPDATE matches
    zero rows (the turn is now ``RUNNING``) and the caller falls back to
    pub/sub.

    Returns ``True`` when an unclaimed turn was cancelled — the caller
    should commit and **skip** the pub/sub publish. Returns ``False`` when
    there was no ``PENDING`` turn (already claimed or terminal) — the
    caller publishes the cancel signal for the running worker instead.
    """
    turn_result = await db.execute(
        update(SessionTurn)
        .where(
            SessionTurn.session_id == session_id,
            SessionTurn.status == SessionTurnStatus.PENDING,
        )
        .values(
            status=SessionTurnStatus.CANCELLED,
            termination_reason=TerminationReason.USER_CANCELLED,
            lease_expires_at=None,
            terminated_at=func.now(),
        )
    )
    if turn_result.rowcount == 0:  # type: ignore[attr-defined]  # UPDATE returns a CursorResult
        # No unclaimed turn — either a worker claimed it (now RUNNING) or
        # it is already terminal. Leave it to the pub/sub cancel path.
        return False

    # Mirror the worker's ``RUNNING``-guarded flip so a racing real
    # terminal write (or operator intervention) isn't clobbered.
    await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.status == SessionStatus.RUNNING)
        .values(status=SessionStatus.CANCELLED)
    )
    # The session is now terminal in this transaction, so the settle
    # helper's terminal-state guard passes; a never-run turn's NULL usage
    # routes it to a full refund of the reservation.
    await settle_open_for_session(db, session_id)
    logger.info(
        "cancel: terminalized unclaimed turn for session %s "
        "(turn + session → CANCELLED, reservation refunded)",
        session_id,
    )
    return True
