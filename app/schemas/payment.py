"""Pydantic schemas for the payment / recharge endpoints.

Money on the wire: the **request** is in RMB (``amount_cny``, what the
user pays); **responses** echo both the RMB amount and the resulting
whole-credit grant (µ-credits are an internal precision detail clients
never see). The QR string is the payment code-URL the frontend renders
as a scannable image.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    """Body of ``POST /payments/orders``.

    ``amount_cny`` bounds are enforced server-side against
    ``payment_min_cny`` / ``payment_max_cny`` (``gt=0`` here is only a
    sanity floor). ``channel`` picks which QR to mint — Alipay or WeChat.
    """

    amount_cny: float = Field(gt=0, description="Top-up amount in RMB")
    channel: Literal["ALIPAY", "WECHAT"]


class CreateOrderResponse(BaseModel):
    """Response of ``POST /payments/orders`` — what the client needs to
    render the QR and start polling."""

    order_id: uuid.UUID
    qr_code: str
    trade_type: str
    credits: int
    expires_at: datetime


class OrderStatusResponse(BaseModel):
    """Response of ``GET /payments/orders/{id}`` — polled until the status
    is terminal (``PAID`` / ``EXPIRED`` / ``FAILED``)."""

    order_id: uuid.UUID
    status: str
    amount_cny: float
    credits: int
    expires_at: datetime
