"""Periodic poll-based settle: backstop for the user-facing lazy sweep.

The user-facing endpoints (``GET /credits``, ``queue_user_message``)
already settle this user's open reservations on every hit — that
covers any user who comes back. The sweeper is the **safety net**
for the "user never comes back" case: API-only clients, abandoned
sessions, worker crashes between RESERVE and the fenced terminal
write.

Sibling to :mod:`app.workers.settle_subscriber` — both run in the
same background process (see ``app/workers/run.py``). They're
the **pull and push** halves of the same settle responsibility:

- sweeper polls the DB every 60s — correctness backstop;
- subscriber listens on ``settle:notify`` pub/sub — latency optimization.

Both call the same shared helpers (``settle_open_for_session`` /
``refund_reservation``); behaviour stays identical to the lazy sweep
on user endpoints.

Process model: standalone sibling process to the web server, not
inside the FastAPI lifespan — see ``app/workers/run.py`` and
``scripts/deploy.sh background``. Web and the background process are
deployed, restarted, and observed independently.

Concurrency: every host in a multi-node deployment runs its own
sweeper. A Postgres **transaction-scoped** advisory lock
(``pg_try_advisory_xact_lock``) ensures only one process actually sweeps
per tick across the entire cluster — the rest see a busy lock and skip.
The lock auto-releases when the tick's transaction ends (normal exit,
crash, SIGKILL, OOM, or connection loss), so it can never strand; the
next tick's first survivor picks it up.

Sweep eligibility (per reservation row, ``state='OPEN'``):

- ``session_id`` is NULL (session was deleted) → refund.
- ``session.status`` is terminal (worker finished, success or fail)
  → delegate to :func:`~app.services.credits.settle_open_for_session`,
  which reads usage from ``session_turn`` and routes to settle (usage
  present) or refund (usage NULL).
- ``expires_at`` has passed AND session not terminal (worker died
  before its terminal flip, or the fleet is down and the turn was never
  claimed) → terminalize the stranded turn + session to FAILED
  (:func:`~app.services.session.recovery.fail_abandoned_turn`) so it
  stops being a zombie, then refund — atomically. Left OPEN if a worker
  still holds a live lease.
- Otherwise (turn still running, not yet expired) → leave open.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.credit_reservation import CreditReservation, CreditReservationState
from app.models.session import Session, SessionStatus
from app.services import credits
from app.services.session.recovery import fail_abandoned_turn

logger = logging.getLogger(__name__)


# 64-bit key for Postgres advisory lock. Bytes ``b"STLSWEEP"`` so
# ``pg_locks`` rows are easy to spot in debugging.
_LOCK_KEY = 0x53544C5357454550

# Max reservations inspected per tick. A bound prevents one
# pathological run (e.g. mass worker crash) from holding the advisory
# lock for minutes; the next tick picks up the rest.
_SWEEP_BATCH = 100

# Session statuses where the turn is still in flight; sweeper leaves
# their reservations alone unless ``expires_at`` has passed.
_NON_TERMINAL = (SessionStatus.IDLE, SessionStatus.RUNNING)


async def run_forever(*, interval_seconds: int = 60) -> None:
    """Periodic sweep loop. Cancel-safe via :class:`asyncio.CancelledError`."""
    consecutive_failures = 0
    while True:
        try:
            processed = await _tick()
            consecutive_failures = 0
            if processed:
                logger.info("sweeper tick: processed=%d", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "sweeper tick failed (consecutive_failures=%d)",
                consecutive_failures,
            )
        # Exponential backoff (capped at 10 min) — protects the DB
        # when something is fundamentally broken without delaying
        # recovery past the point of usefulness.
        delay = (
            min(interval_seconds * (2**consecutive_failures), 600)
            if consecutive_failures
            else interval_seconds
        )
        await asyncio.sleep(delay)


async def _tick() -> int:
    """One sweep cycle. Returns the number of reservations the sweeper
    actually touched (0 when another process held the advisory lock).

    Two distinct session lifetimes here, deliberately separated so a
    per-row failure can't poison the lock session:

    - ``lock_db`` — its transaction stays open for the whole tick to hold
      the advisory lock. Only reads (the candidate list); never writes.
    - Per-row sessions — each candidate reservation gets its own
      :class:`AsyncSession` inside :func:`_process_one`. Isolating
      transactions per row means a DB blip on row 7 doesn't poison
      rows 8-100; the per-row try/except logs and the loop continues.

    The lock is **transaction-scoped** (``pg_try_advisory_xact_lock``): it
    auto-releases when ``lock_db``'s transaction ends as the ``async with``
    block exits (rollback on close). No manual unlock — so there's no path
    where a failed ``pg_advisory_unlock`` on a *pooled* connection leaks a
    session-scoped lock and silently wedges every host's sweeper.
    """
    async with AsyncSessionLocal() as lock_db:
        got = (
            await lock_db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
        ).scalar()
        if not got:
            return 0
        candidates = await _list_candidates(lock_db)
        processed = 0
        for candidate in candidates:
            if await _process_one(candidate):
                processed += 1
        return processed


async def _list_candidates(db: AsyncSession) -> list[tuple]:
    """Read the current sweep-eligible candidates from one snapshot.

    ``Session.status`` is used to *filter* the candidate set but is
    deliberately **not** returned: a 100-row batch is processed serially,
    so by the time ``_process_one`` reaches a late row the snapshot status
    can be many seconds stale (a worker may have committed its terminal +
    usage in between). ``_process_one`` re-reads the live status in its own
    transaction and routes on that, so a completed turn settles instead of
    being wrongly refunded.

    Only rows the sweeper can act on are selected. Leaving eligibility to
    ``_process_one`` after ``LIMIT`` would let a large prefix of active,
    not-yet-expired turns starve later terminal reservations forever.
    """
    rows = await db.execute(
        select(
            CreditReservation.id,
            CreditReservation.session_id,
            CreditReservation.expires_at,
        )
        .join(Session, Session.id == CreditReservation.session_id, isouter=True)
        .where(
            CreditReservation.state == CreditReservationState.OPEN,
            or_(
                CreditReservation.session_id.is_(None),
                Session.status.notin_(_NON_TERMINAL),
                CreditReservation.expires_at < func.now(),
            ),
        )
        .order_by(CreditReservation.created_at.asc(), CreditReservation.id.asc())
        .limit(_SWEEP_BATCH)
    )
    # ``cast`` rather than ``.tuples()``: a ``Row`` is a tuple at runtime but
    # not to a type checker, and reaching for the typed accessor would make
    # production depend on an API this call never needed.
    return cast("list[tuple[Any, ...]]", rows.all())


async def _process_one(candidate: tuple) -> bool:
    """Settle / refund one reservation in its own short transaction.

    Returns ``True`` if the row was actually touched (settled or
    refunded), ``False`` if it was skipped (not eligible) or failed.
    Per-row exceptions are caught and logged — the sweeper must
    survive a single bad reservation to keep processing the batch.
    """
    reservation_id, session_id, expires_at = candidate
    try:
        async with AsyncSessionLocal() as db:
            if session_id is None:
                # Session was deleted; refund.
                await credits.refund_reservation(
                    db,
                    reservation_id=reservation_id,
                    reason={"cause": "sweeper_session_deleted"},
                )
                await db.commit()
                return True
            # Re-read the *live* status — the candidate snapshot is stale by
            # the time we reach a late row in a serial batch, and routing
            # settle-vs-refund off a stale "still running" would refund a
            # turn the worker has since completed (free turn). Decide on the
            # fresh value; the settle / refund helpers below are each
            # idempotent and self-guarding against any remaining TOCTOU.
            session_status = await db.scalar(select(Session.status).where(Session.id == session_id))
            if session_status is not None and session_status not in _NON_TERMINAL:
                # Worker finished — settle from session_turn (or refund
                # if usage is missing, per settle_open_for_session).
                await credits.settle_open_for_session(db, session_id)
                await db.commit()
                return True
            if expires_at is not None and expires_at < datetime.now(UTC):
                # Reservation OPEN past its 4h expiry on a non-terminal
                # session: the worker is gone (crashed before its terminal
                # write, or the fleet is down and the turn was never
                # claimed). Terminalize the stranded turn + session to FAILED
                # so it stops being a zombie, THEN refund — same commit, so
                # lifecycle and money reconcile atomically. A False return
                # means a worker still holds a live lease; leave it OPEN for
                # the next sweep rather than refund a turn that may finish.
                if not await fail_abandoned_turn(db, session_id=session_id):
                    return False
                await credits.refund_reservation(
                    db,
                    reservation_id=reservation_id,
                    reason={"cause": "sweeper_expired_no_terminal"},
                )
                await db.commit()
                return True
            # Still in-flight; leave alone.
            return False
    except Exception:
        logger.exception(
            "sweeper: failed to process reservation %s",
            reservation_id,
        )
        return False
