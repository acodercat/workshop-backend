"""Periodic poll-based settle: backstop for the user-facing lazy sweep.

The user-facing endpoints (``GET /credits``, ``queue_user_message``)
already settle this user's open reservations on every hit — that
covers any user who comes back. The sweeper is the **safety net**
for the "user never comes back" case: API-only clients, abandoned
sessions, worker crashes between RESERVE and the fenced terminal
write.

Sibling to :mod:`app.workers.settle_subscriber` — both run in the
same background-settle process (see ``app/workers/run.py``). They're
the **pull and push** halves of the same settle responsibility:

- sweeper polls the DB every 60s — correctness backstop;
- subscriber listens on ``settle:notify`` pub/sub — latency optimization.

Both call the same shared helpers (``settle_open_for_session`` /
``refund_reservation``); behaviour stays identical to the lazy sweep
on user endpoints.

Process model: standalone sibling process to the web server, not
inside the FastAPI lifespan — see ``app/workers/run.py`` and
``scripts/deploy.sh settle``. Web and background-settle are deployed,
restarted, and observed independently.

Concurrency: every host in a multi-node deployment runs its own
sweeper. Postgres advisory locks (:func:`pg_try_advisory_lock`) ensure
only one process actually sweeps per tick across the entire cluster
— the rest see a busy lock and skip. Connection loss = automatic
release, so a crashed sweeper (SIGKILL, OOM, hardware failure) never
strands the lock; the next tick's first survivor picks it up.

Sweep eligibility (per reservation row, ``state='OPEN'``):

- ``session_id`` is NULL (session was deleted) → refund.
- ``session.status`` is terminal (worker finished, success or fail)
  → delegate to :func:`~app.services.credits.settle_open_for_session`,
  which reads usage from ``session_turn`` and routes to settle (usage
  present) or refund (usage NULL).
- ``expires_at`` has passed AND session not terminal (worker died
  between RESERVE and terminal flip) → refund.
- Otherwise (turn still running, not yet expired) → leave open.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.credit_reservation import CreditReservation, CreditReservationState
from app.models.session import Session, SessionStatus
from app.services import credits

logger = logging.getLogger(__name__)


# 64-bit key for Postgres advisory lock. Bytes ``b"STLSWEEP"`` so
# ``pg_locks`` rows are easy to spot in debugging.
_LOCK_KEY = 0x53544c5357454550

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
            min(interval_seconds * (2 ** consecutive_failures), 600)
            if consecutive_failures
            else interval_seconds
        )
        await asyncio.sleep(delay)


async def _tick() -> int:
    """One sweep cycle. Returns the number of reservations the sweeper
    actually touched (0 when another process held the advisory lock).

    Two distinct session lifetimes here, deliberately separated so a
    per-row failure can't poison the lock session:

    - ``lock_db`` — held for the whole tick to keep the advisory lock
      acquired. Only reads (the candidate list); never writes. Its
      transaction state can't be aborted by per-row failures.
    - Per-row sessions — each candidate reservation gets its own
      :class:`AsyncSession` inside :func:`_process_one`. Isolating
      transactions per row means a DB blip on row 7 doesn't poison
      rows 8-100; the per-row try/except logs and the loop continues.
    """
    async with AsyncSessionLocal() as lock_db:
        got = (
            await lock_db.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
            )
        ).scalar()
        if not got:
            return 0
        try:
            candidates = await _list_candidates(lock_db)
            processed = 0
            for candidate in candidates:
                if await _process_one(candidate):
                    processed += 1
            return processed
        finally:
            await _release_lock(lock_db)


async def _list_candidates(db: AsyncSession) -> list[tuple]:
    """Read the current sweep-eligible candidates from one snapshot.

    Returned tuples are immutable — the per-row workers re-fetch under
    ``FOR UPDATE`` inside their own transactions, so a candidate that
    raced and got settled between list-time and process-time becomes
    a harmless no-op (settle is idempotent on terminal state).

    Only rows the sweeper can act on are selected. Leaving eligibility to
    ``_process_one`` after ``LIMIT`` would let a large prefix of active,
    not-yet-expired turns starve later terminal reservations forever.
    """
    rows = await db.execute(
        select(
            CreditReservation.id,
            CreditReservation.session_id,
            CreditReservation.expires_at,
            Session.status,
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
    return list(rows.all())


async def _release_lock(db: AsyncSession) -> None:
    """Best-effort unlock — connection close also releases, so a
    failure here is logged but never re-raised."""
    try:
        await db.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
        )
    except Exception:
        logger.exception("sweeper: pg_advisory_unlock failed (connection-close fallback will release)")


async def _process_one(candidate: tuple) -> bool:
    """Settle / refund one reservation in its own short transaction.

    Returns ``True`` if the row was actually touched (settled or
    refunded), ``False`` if it was skipped (not eligible) or failed.
    Per-row exceptions are caught and logged — the sweeper must
    survive a single bad reservation to keep processing the batch.
    """
    reservation_id, session_id, expires_at, session_status = candidate
    try:
        async with AsyncSessionLocal() as db:
            if session_id is None:
                # Session was deleted; refund.
                await credits.refund_reservation(
                    db, reservation_id=reservation_id,
                    reason={"cause": "sweeper_session_deleted"},
                )
                await db.commit()
                return True
            session_terminal = (
                session_status is not None and session_status not in _NON_TERMINAL
            )
            if session_terminal:
                # Worker finished — settle from session_turn (or refund
                # if usage is missing, per settle_open_for_session).
                await credits.settle_open_for_session(db, session_id)
                await db.commit()
                return True
            if expires_at is not None and expires_at < datetime.now(timezone.utc):
                # Worker died between RESERVE and the fenced terminal
                # write — session.status stays RUNNING / IDLE, no usage
                # on session_turn. Refund.
                await credits.refund_reservation(
                    db, reservation_id=reservation_id,
                    reason={"cause": "sweeper_expired_no_terminal"},
                )
                await db.commit()
                return True
            # Still in-flight; leave alone.
            return False
    except Exception:
        logger.exception(
            "sweeper: failed to process reservation %s", reservation_id,
        )
        return False
