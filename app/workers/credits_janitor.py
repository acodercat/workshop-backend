"""Background sweep for credit reservations missed by the user-facing
lazy sweep.

The user-facing endpoints (``GET /credits``, ``queue_user_message``)
already settle this user's open reservations on every hit — that
covers any user who comes back. The janitor is the **safety net** for
the "user never comes back" case: API-only clients, abandoned
sessions, worker crashes between RESERVE and the fenced terminal
write.

Process model: the janitor runs as a **standalone sibling process**
to the web server, not inside the FastAPI lifespan — see
``app/workers/run.py`` and ``scripts/deploy.sh janitor``. Web and
janitor are deployed, restarted, and observed independently.

Concurrency: every host in a multi-node deployment runs its own
janitor process. Postgres advisory locks
(:func:`pg_try_advisory_lock`) ensure only one process actually
sweeps per tick across the entire cluster — the rest see a busy
lock and skip. Connection loss = automatic release, so a crashed
sweeper (SIGKILL, OOM, hardware failure) never strands the lock;
the next tick's first survivor picks it up.

Sweep eligibility (per reservation row, ``state='OPEN'``):

- ``session_id`` is NULL (session was deleted) → refund.
- ``session.status`` is terminal (worker finished, success or fail)
  → delegate to :func:`~app.services.credits.settle_open_for_session`,
  which reads usage from ``session_turn`` and routes to settle (usage
  present) or refund (usage NULL).
- ``expires_at`` has passed AND session not terminal (worker died
  between RESERVE and terminal flip) → refund.
- Otherwise (turn still running, not yet expired) → leave open.

The janitor is a **trigger** for the shared settle helper, not a
parallel implementation — behaviour stays identical to the lazy sweep.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.credit_reservation import CreditReservation, CreditReservationState
from app.models.session import Session, SessionStatus
from app.services import credits

logger = logging.getLogger(__name__)


# 64-bit key for Postgres advisory lock. Bytes ``b"CREDJANI"`` so
# ``pg_locks`` rows are easy to spot in debugging.
_LOCK_KEY = 0x4352454447414e49

# Max reservations inspected per tick. A bound prevents one
# pathological run (e.g. mass worker crash) from holding the advisory
# lock for minutes; the next tick picks up the rest.
_SWEEP_BATCH = 100

# Session statuses where the turn is still in flight; janitor leaves
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
                logger.info("janitor tick: processed=%d", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "janitor tick failed (consecutive_failures=%d)",
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
    """One sweep cycle. Returns the number of reservations the janitor
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
    """
    rows = await db.execute(
        select(
            CreditReservation.id,
            CreditReservation.session_id,
            CreditReservation.expires_at,
            Session.status,
        )
        .join(Session, Session.id == CreditReservation.session_id, isouter=True)
        .where(CreditReservation.state == CreditReservationState.OPEN)
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
        logger.exception("janitor: pg_advisory_unlock failed (connection-close fallback will release)")


async def _process_one(candidate: tuple) -> bool:
    """Settle / refund one reservation in its own short transaction.

    Returns ``True`` if the row was actually touched (settled or
    refunded), ``False`` if it was skipped (not eligible) or failed.
    Per-row exceptions are caught and logged — the janitor must
    survive a single bad reservation to keep processing the batch.
    """
    reservation_id, session_id, expires_at, session_status = candidate
    try:
        async with AsyncSessionLocal() as db:
            if session_id is None:
                # Session was deleted; refund.
                await credits.refund_reservation(
                    db, reservation_id=reservation_id,
                    reason={"cause": "janitor_session_deleted"},
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
                    reason={"cause": "janitor_expired_no_terminal"},
                )
                await db.commit()
                return True
            # Still in-flight; leave alone.
            return False
    except Exception:
        logger.exception(
            "janitor: failed to process reservation %s", reservation_id,
        )
        return False
