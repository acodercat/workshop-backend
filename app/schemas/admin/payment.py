"""Response models for the console's payments view.

Money on the wire is **yuan** (`*_cny`, two decimals) because this is
the RMB side of the platform — the same convention the customer-facing
commission endpoints use. Credits stay whole credits. Both appear on an
order row on purpose: what the customer paid beside what they received
is the only place the recharge margin is visible.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.payment_order import PaymentOrderStatus


class DailyRevenuePoint(BaseModel):
    """One business day of the revenue series."""

    day: date
    revenue_cny: float


class RevenueStatsResponse(BaseModel):
    """The payments page's tiles and chart for one window.

    ``pending_orders`` is deliberately **not** windowed: an order stuck
    awaiting payment since last week is exactly the one worth seeing,
    and a trailing window would hide the oldest cases first.
    """

    days: int
    revenue_cny: float
    paid_orders: int
    created_orders: int
    granted_credits: int
    pending_orders: int
    daily: list[DailyRevenuePoint]


class PaymentOrderResponse(BaseModel):
    """One recharge order.

    ``out_trade_no`` is the provider-facing key an operator quotes when
    asking 汇付 about a payment; ``provider_txn_id`` is their side of it,
    present only once paid.
    """

    id: UUID
    user_id: int
    owner_username: str
    provider: str
    out_trade_no: str
    provider_txn_id: str | None
    amount_cny: float
    credits: int
    status: PaymentOrderStatus
    ledger_id: int | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
