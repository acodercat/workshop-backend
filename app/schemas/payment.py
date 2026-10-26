"""Pydantic schemas for the payment / recharge endpoints.

Money on the wire: the **request** is in RMB (``amount_cny``, what the
user pays); **responses** echo both the RMB amount and the resulting
whole-credit grant (µ-credits are an internal precision detail clients
never see). The QR string is the payment code-URL the frontend renders
as a scannable image.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    """Body of ``POST /payments/orders``.

    ``amount_cny`` bounds are enforced server-side against
    ``payment_min_cny`` / ``payment_max_cny`` (``gt=0`` here is only a
    sanity floor). At most two decimals, so the cents charged and the
    credits granted are both exact functions of one amount rather than
    two roundings of it. ``channel`` picks which QR to mint — Alipay or
    WeChat.
    """

    amount_cny: Decimal = Field(gt=0, decimal_places=2, description="Top-up amount in RMB")
    channel: Literal["ALIPAY", "WECHAT"]


class CreateOrderResponse(BaseModel):
    """Response of ``POST /payments/orders`` — what the client needs to
    render the QR and start polling."""

    order_id: uuid.UUID
    qr_code: str
    trade_type: str
    # ``credits`` is the full grant that lands on PAID (paid base + promo
    # bonus); ``bonus_credits`` is the bonus portion, for the "incl. N bonus"
    # line. ``bonus_credits == 0`` when the promo is off.
    credits: int
    bonus_credits: int
    expires_at: datetime


class OrderStatusResponse(BaseModel):
    """Response of ``GET /payments/orders/{id}`` — polled until the status
    is terminal (``PAID`` / ``EXPIRED`` / ``FAILED``). ``status`` is the
    stored status: an unpaid order stays ``PENDING`` past ``expires_at``
    until the reconciler settles it against the provider, so a client
    showing a dead QR derives that from ``expires_at`` itself rather than
    waiting for this field to change."""

    order_id: uuid.UUID
    status: str
    amount_cny: float
    credits: int
    expires_at: datetime


class PaymentOrderEntry(BaseModel):
    """One row of the recharge / billing history (``GET /payments/orders``).

    Only PAID orders are listed — the actual recharges, not abandoned QR
    sessions. ``channel`` is the pay method (``ALIPAY`` / ``WECHAT``);
    ``out_trade_no`` is the provider-facing order id for reconciliation /
    support."""

    order_id: uuid.UUID
    amount_cny: float
    credits: int
    channel: str
    out_trade_no: str
    created_at: datetime
