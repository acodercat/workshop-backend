"""The console's payments view: recharge revenue and the order list.

- ``GET /payments/stats?days=`` — revenue tiles + daily series.
- ``GET /payments?status=&user_id=`` — orders, newest first.

**Read-only, deliberately.** There is no "settle this order" button:
``app.workers.payment_reconciler`` already polls the provider for
`PENDING` orders and grants credits idempotently through the same
``_grant_paid`` path the webhook uses. A console button would be a
second way into a money path that already self-heals — the gap this
page fills is *seeing* a stuck order, not poking it.

Aggregates live in :mod:`app.services.payment.analytics` (the domain
owns questions about its own table), keeping
:mod:`app.api.admin.stats` about the credit ledger as its docstring
says. Not audited — pure reads over money that has already moved.
"""

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.api.admin.window import WindowDays
from app.common.pagination import paginate
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.models.user import User
from app.schemas.admin.payment import (
    DailyRevenuePoint,
    PaymentOrderResponse,
    RevenueStatsResponse,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services.commission.rates import cny_from_fen
from app.services.credits.rates import credits_from_micro
from app.services.payment import analytics

router = APIRouter(tags=["payments"])


def _to_order_response(order: PaymentOrder, owner_username: str) -> PaymentOrderResponse:
    return PaymentOrderResponse(
        id=order.id,
        user_id=order.user_id,
        owner_username=owner_username,
        provider=order.provider.value,
        out_trade_no=order.out_trade_no,
        provider_txn_id=order.provider_txn_id,
        amount_cny=cny_from_fen(order.amount_fen),
        credits=credits_from_micro(order.credit_micro),
        status=order.status,
        ledger_id=order.ledger_id,
        expires_at=order.expires_at,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


@router.get("/payments/stats", response_model=RevenueStatsResponse)
async def read_revenue_stats(
    ctx: StaffContext,
    days: WindowDays,
) -> RevenueStatsResponse:
    """Return recharge revenue for the trailing window.

    Revenue counts ``PAID`` orders only — a pending QR code is an
    intention, not cash.
    """
    summary = await analytics.summarize(ctx.db, days=days)
    return RevenueStatsResponse(
        days=days,
        revenue_cny=cny_from_fen(summary.revenue_fen),
        paid_orders=summary.paid_orders,
        created_orders=summary.created_orders,
        granted_credits=credits_from_micro(summary.granted_micro),
        pending_orders=summary.pending_orders,
        daily=[
            DailyRevenuePoint(day=point.day, revenue_cny=cny_from_fen(point.revenue_fen))
            for point in summary.daily
        ],
    )


@router.get("/payments", response_model=PaginatedResponse[PaymentOrderResponse])
async def list_payments(
    ctx: StaffContext,
    status: PaymentOrderStatus | None = Query(None),
    user_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[PaymentOrderResponse]:
    """Return one page of recharge orders, newest first.

    ``?status=PENDING`` is the operational view — orders where money may
    have moved but credits have not yet followed.
    """
    filters = []
    if status is not None:
        filters.append(PaymentOrder.status == status)
    if user_id is not None:
        filters.append(PaymentOrder.user_id == user_id)

    total = await ctx.db.scalar(select(func.count()).select_from(PaymentOrder).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(PaymentOrder, User.username)
                .join(User, User.id == PaymentOrder.user_id)
                .where(*filters)
                .order_by(PaymentOrder.created_at.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[_to_order_response(order, username) for order, username in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
