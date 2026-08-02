"""Payment / recharge endpoints (汇付斗拱).

Three surfaces:

- ``POST /payments/orders`` — authenticated; create an order + return a
  QR to scan. Rate-limited per IP.
- ``POST /payments/webhooks/huifu`` — **unauthenticated**; the provider's
  async notification. Authenticated instead by RSA signature over the raw
  body. Exempt from rate limiting (the provider retries from its own IPs)
  and answered with the exact ``RECV_ORD_ID_…`` ack the provider expects —
  but only after the payment is durably recorded, so a processing failure
  lets the provider redeliver.
- ``GET /payments/orders/{id}`` — authenticated poll for the order status.

The webhook reads the raw urlencoded body itself (``parse_qs``) rather
than ``request.form()`` so the exact ``resp_data`` string that was signed
is what we verify — and so the route needs no multipart dependency.
"""

import logging
import urllib.parse
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.errors import PaymentDisabledError
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.payment import (
    CreateOrderRequest,
    CreateOrderResponse,
    OrderStatusResponse,
    PaymentOrderEntry,
)
from app.services import commission, payment
from app.services.credits.rates import credits_from_micro
from app.services.payment.exceptions import NotifySignatureError
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_enabled() -> None:
    """Gate the user-facing endpoints behind the master switch."""
    if not settings.payment_enabled:
        raise PaymentDisabledError("Payments are not enabled")


@router.post("/payments/orders", response_model=CreateOrderResponse)
@limiter.limit("10/minute")
async def create_payment_order(
    request: Request,
    body: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> CreateOrderResponse:
    """Create a recharge order and return its QR.

    The credit grant is frozen at the current sale price on the order, so
    a price change before the user pays can't alter what they receive.
    """
    _ensure_enabled()
    order = await payment.create_order(
        db, user_id=current_user.id, amount_cny=body.amount_cny, channel=body.channel,
    )
    await db.commit()
    return CreateOrderResponse(
        order_id=order.id,
        qr_code=order.reference["qr_code"],
        trade_type=order.reference["trade_type"],
        credits=credits_from_micro(order.credit_micro),
        bonus_credits=order.reference.get("bonus_credits", 0),
        expires_at=order.expires_at,
    )


@router.get(
    "/payments/orders", response_model=PaginatedResponse[PaymentOrderEntry],
)
async def list_payment_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[PaymentOrderEntry]:
    """Recharge / billing history — the caller's PAID orders, newest first.

    Not gated by ``payment_enabled``: viewing past recharges stays
    available even if new top-ups are turned off.
    """
    rows, total = await payment.list_paid_orders(
        db, user_id=current_user.id, page=page, page_size=page_size,
    )
    return PaginatedResponse(
        items=[
            PaymentOrderEntry(
                order_id=o.id,
                amount_cny=o.amount_fen / 100,
                credits=credits_from_micro(o.credit_micro),
                channel=o.reference.get("channel", ""),
                out_trade_no=o.out_trade_no,
                created_at=o.created_at,
            )
            for o in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post("/payments/webhooks/huifu", include_in_schema=False)
@limiter.exempt
async def huifu_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Handle a 汇付 async payment notification.

    Verifies the RSA signature over the raw ``resp_data`` string, applies
    the verified outcome idempotently, and acks. Never trusts an
    unverifiable body (400, no ack); on a processing failure returns 500
    (no ack) so the provider redelivers.
    """
    raw = await request.body()
    parsed = urllib.parse.parse_qs(raw.decode("utf-8"))
    resp_data = parsed.get("resp_data", [""])[0]
    sign = parsed.get("sign", [""])[0]
    if not resp_data or not sign:
        logger.warning("huifu webhook: missing resp_data/sign")
        return PlainTextResponse("FAIL", status_code=400)

    provider = payment.get_provider()
    try:
        event = provider.verify_notify(resp_data_raw=resp_data, sign=sign)
    except NotifySignatureError:
        logger.warning("huifu webhook: signature verification failed")
        return PlainTextResponse("FAIL", status_code=400)

    logger.info(
        "huifu webhook received: out_trade_no=%s succeeded=%s",
        event.out_trade_no, event.succeeded,
    )
    try:
        await payment.handle_webhook(db, event)
        await db.commit()
    except Exception:
        # Durability failure — do NOT ack, let 汇付 redeliver (the handler
        # is idempotent, so a retry is safe). get_db rolls the tx back.
        logger.exception("huifu webhook: processing failed for %s", event.out_trade_no)
        return PlainTextResponse("FAIL", status_code=500)

    # Best-effort referral commission, after the top-up is durably committed.
    # Self-gates on PAID status + own transaction, so it never affects the
    # buyer's recharge or the ack (idempotent across the provider's retries).
    await commission.accrue_for_order(db, out_trade_no=event.out_trade_no)

    return PlainTextResponse(provider.ack(event.out_trade_no))


@router.get("/payments/orders/{order_id}", response_model=OrderStatusResponse)
async def get_payment_order(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> OrderStatusResponse:
    """Poll one order's status (lazily expiring it if its TTL elapsed)."""
    order = await payment.get_order(db, order_id=order_id, user_id=current_user.id)
    await db.commit()  # persist a lazy expiry
    return OrderStatusResponse(
        order_id=order.id,
        status=order.status.value,
        amount_cny=order.amount_fen / 100,
        credits=credits_from_micro(order.credit_micro),
        expires_at=order.expires_at,
    )
