"""Per-order reconciliation — the pull half of payment settlement.

The async notification is the fast path; this is the backstop for a
notification that was lost or never sent (provider down, our box briefly
unreachable). For one PENDING order it asks the provider for the live
state and either settles it (paid) or expires it (TTL elapsed). Driven by
:mod:`app.workers.payment_reconciler`. Caller-agnostic: opens nothing,
commits its own per-order transaction.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import PaymentProviderError
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services.payment import orders

logger = logging.getLogger(__name__)


async def reconcile_order(db: AsyncSession, order_id: uuid.UUID) -> bool:
    """Reconcile one order against the provider. Returns ``True`` if the
    order was settled or expired, ``False`` if left PENDING (still in
    flight). Commits on a state change.
    """
    order = await db.scalar(
        select(PaymentOrder)
        .where(PaymentOrder.id == order_id)
        .with_for_update()
    )
    if order is None or order.status != PaymentOrderStatus.PENDING:
        return False  # already settled/expired by the webhook or a prior tick

    req_date = order.reference.get("req_date", "")
    event = None
    try:
        event = await orders.get_provider().query_charge(
            out_trade_no=order.out_trade_no, req_date=req_date,
        )
    except PaymentProviderError:
        # Query failed (e.g. order not yet known to the provider). Fall
        # through to the expiry check so a stale unpaid order still reaps.
        logger.info("reconcile: query failed for order=%s", order.id)

    if event is not None:
        logger.info(
            "reconcile order=%s query: succeeded=%s amount_fen=%s",
            order.id, event.succeeded, event.amount_fen,
        )

    if event is not None and event.succeeded:
        await orders.reconcile_paid(db, order, event)
        await db.commit()
        return True

    if orders.is_expired(order, datetime.now(timezone.utc)):
        order.status = PaymentOrderStatus.EXPIRED
        await db.commit()
        return True

    return False
