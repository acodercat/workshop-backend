"""Periodic payment reconciliation — the pull backstop for lost async
notifications.

The 汇付 async notification is the fast settle path; this poller catches
orders whose notification was lost or never sent, and reaps orders whose
TTL elapsed unpaid. It mirrors :mod:`app.workers.settle_sweeper`: every
host runs it, a Postgres **transaction-scoped** advisory lock
(``pg_try_advisory_xact_lock``) elects exactly one poller per tick, and
the lock auto-releases when the tick's transaction ends (normal exit,
crash, SIGKILL, connection loss) so it can never strand.

Each PENDING order older than one poll interval (giving the webhook time
to land first) is checked against the provider in its own short
transaction — a bad row can't poison the batch. ``reconcile_order``
settles a paid order (idempotent with the webhook), fails a failed one,
or expires one the provider gave no verdict on once its window closes.
Orders the provider has no verdict on yet stay PENDING for up to a day,
so a tick's batch is taken least-recently-attempted first: a full batch
of them cannot stand between the poller and a newer order whose
notification was lost.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services import payment
from config import settings

logger = logging.getLogger(__name__)


# 64-bit key for the Postgres advisory lock. Bytes ``b"PAYRECON"`` so
# ``pg_locks`` rows are easy to spot in debugging (distinct from the
# settle sweeper's ``b"STLSWEEP"``).
_LOCK_KEY = int.from_bytes(b"PAYRECON", "big")

# Max orders inspected per tick — bounds how long one run can hold the
# advisory lock; the next tick picks up the rest.
_SWEEP_BATCH = 100


async def run_forever(
    *,
    interval_seconds: int = settings.payment_reconcile_interval_seconds,
) -> None:
    """Periodic reconcile loop. Cancel-safe via :class:`asyncio.CancelledError`."""
    consecutive_failures = 0
    while True:
        try:
            processed = await _tick()
            consecutive_failures = 0
            if processed:
                logger.info("payment_reconciler tick: processed=%d", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "payment_reconciler tick failed (consecutive_failures=%d)",
                consecutive_failures,
            )
        # Exponential backoff capped at 10 min — same shape as the
        # settle sweeper.
        delay = (
            min(interval_seconds * (2**consecutive_failures), 600)
            if consecutive_failures
            else interval_seconds
        )
        await asyncio.sleep(delay)


async def _tick() -> int:
    """One reconcile cycle. Returns the number of orders that **left**
    ``PENDING`` — settled, failed or expired (0 when another host held the
    advisory lock). Every candidate is queried and stamped whether or not
    it moves, so a quiet return does not mean a quiet tick; the per-order
    INFO lines in :func:`~app.services.payment.reconcile.reconcile_order`
    are where that work is visible.

    Like the settle sweeper, the lock session stays read-only for the
    whole tick (holding the advisory lock); each candidate is processed
    in its own short transaction so a per-row failure can't poison the
    rest of the batch.
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
        for order_id in candidates:
            if await _process_one(order_id):
                processed += 1
        return processed


async def _list_candidates(db: AsyncSession) -> list:
    """PENDING orders old enough that a notification should already have
    arrived, least recently attempted first — never tried before that.
    The ``created_at`` grace (one poll interval) keeps the poller from
    racing the webhook on brand-new orders; the ``status='PENDING'``
    partial index keeps this scan tiny."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.payment_reconcile_interval_seconds)
    rows = await db.execute(
        select(PaymentOrder.id)
        .where(
            PaymentOrder.status == PaymentOrderStatus.PENDING,
            PaymentOrder.created_at < cutoff,
        )
        .order_by(PaymentOrder.reconcile_attempted_at.asc().nulls_first())
        .limit(_SWEEP_BATCH)
    )
    return [row[0] for row in rows.all()]


async def _process_one(order_id) -> bool:
    """Reconcile one order in its own transaction. Per-row exceptions are
    caught and logged so the poller survives a single bad order."""
    try:
        async with AsyncSessionLocal() as db:
            return await payment.reconcile_order(db, order_id)
    except Exception:
        logger.exception("payment_reconciler: failed to process order %s", order_id)
        return False
