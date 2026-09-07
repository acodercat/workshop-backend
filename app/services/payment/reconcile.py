"""Per-order reconciliation — the pull half of payment settlement.

The async notification is the fast path; this is the backstop for a
notification that was lost or never sent (provider down, our box briefly
unreachable). For one PENDING order it asks the provider for the live
state and applies the verdict — PAID settles, FAILED fails — or, with no
verdict (PROCESSING, or no answer), leaves the order PENDING to be asked
about again until the window for that kind of non-answer closes past
``expires_at``, when it is expired with what was known recorded
(:func:`orders.expiry_due` / :func:`orders.expire`). EXPIRED is *decided*
here and nowhere else — the assignment itself lives with the rest of the
state machine in ``orders.py``. Every attempt is stamped on the row so the poller
can round-robin the PENDING set. Driven by
:mod:`app.workers.payment_reconciler`. Caller-agnostic: opens nothing,
commits its own per-order transaction.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import PaymentProviderError
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services import commission
from app.services.payment import orders
from app.services.payment.provider import ChargeState, VerifiedEvent

logger = logging.getLogger(__name__)


async def reconcile_order(db: AsyncSession, order_id: uuid.UUID) -> bool:
    """Reconcile one order against the provider. Returns ``True`` if the
    order left PENDING (settled, failed or expired), ``False`` if it is
    still in flight. Commits every attempt: the attempt itself is
    recorded (``reconcile_attempted_at``), state change or not.
    """
    order = await db.scalar(
        select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
    )
    if order is None or order.status != PaymentOrderStatus.PENDING:
        return False  # already settled/expired by the webhook or a prior tick

    now = datetime.now(UTC)
    order.reconcile_attempted_at = now
    event = await _query(order)
    if event is not None and event.state is ChargeState.PAID:
        # Capture the provider key before commit — ORM attrs expire on
        # commit, and commission accrual (post-commit, own tx) takes a
        # primitive so it re-loads the order fresh.
        out_trade_no = order.out_trade_no
        await orders.reconcile_paid(db, order, event)
        await db.commit()
        await commission.accrue_for_order(db, out_trade_no=out_trade_no)
        return True
    if event is not None and event.state is ChargeState.FAILED:
        orders.fail(order, event, via="query")
        await db.commit()
        return True

    # No verdict. Past the window for this kind of non-answer, expire;
    # before it, the stamp is all that changes.
    answered = event is not None
    due = orders.expiry_due(order, now, answered=answered)
    if due:
        orders.expire(order, now=now, answered=answered)
    await db.commit()
    return due


async def _query(order: PaymentOrder) -> VerifiedEvent | None:
    """The provider's live answer for ``order``, or ``None`` when there is
    none — unreachable, or the gateway rejected the query. No answer says
    nothing about whether the order was paid: the caller treats it as
    unknown, never as unpaid."""
    try:
        event = await orders.get_provider().query_charge(
            out_trade_no=order.out_trade_no,
            req_date=order.reference.get("req_date", ""),
        )
    except PaymentProviderError:
        logger.info("reconcile: query failed for order=%s", order.id)
        return None
    logger.info(
        "reconcile order=%s query: state=%s amount_fen=%s",
        order.id,
        event.state,
        event.amount_fen,
    )
    return event
