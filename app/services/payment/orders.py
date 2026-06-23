"""Payment-order state machine.

Pure business logic over ``payment_order`` + the credit ledger. Every
function takes an ``AsyncSession`` and **does not commit** — the caller
(endpoint / worker) owns the transaction, matching the ``credits``
service contract.

Credits move only through ``credits.apply_topup`` (idempotent on
``"hf:"+out_trade_no``); this module never touches ``credit_ledger`` or
``user.credit_balance`` directly. The webhook and the reconciler share
:func:`_grant_paid`, so a payment confirmed by either path grants credits
exactly once even if both fire (row lock + status guard + topup
idempotency).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import OrderNotFoundError, PaymentAmountRangeError
from app.models.credit_ledger import CreditLedger
from app.models.payment_order import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentProvider,
)
from app.services.credits import TopupAlreadyApplied, apply_topup
from app.services.credits.rates import credits_from_cny, micro_from_credits
from app.services.payment.huifu import HuifuGateway
from app.services.payment.provider import Channel, PaymentGateway, VerifiedEvent
from config import settings

logger = logging.getLogger(__name__)

# Beijing time — Huifu's ``req_date`` (yyyyMMdd) is a Beijing calendar day;
# it must match between create and the later reconciliation query.
_CN_TZ = ZoneInfo("Asia/Shanghai")

# One process-wide provider instance (stateless; the SDK client it wraps is
# a process-global initialised lazily on first call).
_provider: PaymentGateway = HuifuGateway()

_TOPUP_REASON = "recharge"

# Grace past ``expires_at`` before an order is reaped EXPIRED. A payment that
# settles right at the deadline (still "processing" on the first poll) is then
# caught by the reconciler's query path rather than stranded as EXPIRED if its
# async notification is lost. Shared by the lazy read-path expiry and the
# reconciler so the two never disagree on when an order is dead.
_EXPIRE_GRACE_SECONDS = 180


def is_expired(order: PaymentOrder, now: datetime) -> bool:
    """True once an order is past its expiry **plus grace** — safe to reap."""
    return order.expires_at < now - timedelta(seconds=_EXPIRE_GRACE_SECONDS)


def get_provider() -> PaymentGateway:
    """The process-wide payment provider (used by the webhook endpoint to
    verify/ack and by the reconciler to query)."""
    return _provider


def _topup_key(out_trade_no: str) -> str:
    """The ``apply_topup`` idempotency key for an order. Shared by the
    webhook and the reconciler so the two settle paths can't drift."""
    return "hf:" + out_trade_no


def _excerpt(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim a provider payload to the fields worth keeping for audit —
    avoids storing bulky / sensitive blobs in ``reference``."""
    keep = (
        "resp_code", "resp_desc", "sub_resp_code", "sub_resp_desc",
        "hf_seq_id", "trans_stat", "trans_amt",
    )
    return {k: raw[k] for k in keep if k in raw}


async def create_order(
    db: AsyncSession,
    *,
    user_id: int,
    amount_cny: float,
    channel: Channel,
) -> PaymentOrder:
    """Create a PENDING order, freeze the credit grant, and place the
    provider charge. Returns the persisted order (with ``reference.qr_code``
    set). Raises :class:`PaymentAmountRangeError` if the amount is out of
    bounds; the caller commits.
    """
    if not (settings.payment_min_cny <= amount_cny <= settings.payment_max_cny):
        raise PaymentAmountRangeError(
            f"Top-up amount ¥{amount_cny} outside "
            f"[¥{settings.payment_min_cny}, ¥{settings.payment_max_cny}]"
        )

    amount_fen = int((Decimal(str(amount_cny)) * 100).to_integral_value())
    credit_micro = micro_from_credits(credits_from_cny(amount_cny))
    out_trade_no = "hf" + uuid.uuid4().hex
    # Huifu wants Beijing-time strings derived from the same instant as our
    # ``expires_at``: ``req_date`` is the calendar day, ``time_expire`` the
    # QR deadline — so Huifu stops accepting payment exactly when the order
    # expires (no "paid after local expiry" window).
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.payment_order_ttl_seconds)
    req_date = now.astimezone(_CN_TZ).strftime("%Y%m%d")
    time_expire = expires_at.astimezone(_CN_TZ).strftime("%Y%m%d%H%M%S")

    order = PaymentOrder(
        user_id=user_id,
        provider=PaymentProvider.HUIFU,
        out_trade_no=out_trade_no,
        amount_fen=amount_fen,
        credit_micro=credit_micro,
        status=PaymentOrderStatus.PENDING,
        reference={"req_date": req_date, "channel": channel},
        expires_at=expires_at,
    )
    db.add(order)
    await db.flush()  # enforce out_trade_no uniqueness before the provider call

    charge = await _provider.create_charge(
        out_trade_no=out_trade_no,
        amount_fen=amount_fen,
        channel=channel,
        goods_desc="Credits top-up",
        req_date=req_date,
        time_expire=time_expire,
    )
    order.provider_txn_id = charge.provider_txn_id
    order.reference = {
        **order.reference,
        "trade_type": charge.trade_type,
        "qr_code": charge.qr_code,
        "create_resp": _excerpt(charge.raw),
    }
    return order


async def get_order(
    db: AsyncSession, *, order_id: uuid.UUID, user_id: int,
) -> PaymentOrder:
    """Fetch a caller-owned order, lazily expiring it if its TTL elapsed
    while still PENDING. Raises :class:`OrderNotFoundError` (folds
    not-owned into not-found). Caller commits to persist a lazy expiry."""
    order = await db.scalar(
        select(PaymentOrder).where(
            PaymentOrder.id == order_id, PaymentOrder.user_id == user_id,
        )
    )
    if order is None:
        raise OrderNotFoundError(f"Order {order_id} not found")
    if order.status == PaymentOrderStatus.PENDING and is_expired(
        order, datetime.now(timezone.utc)
    ):
        order.status = PaymentOrderStatus.EXPIRED
    return order


async def handle_webhook(db: AsyncSession, event: VerifiedEvent) -> None:
    """Apply a verified async notification to its order (idempotent).

    The signature has already been verified by the provider adapter; this
    only routes the verified outcome. Safe under at-least-once delivery:
    the row lock + the PAID guard + topup idempotency make a duplicate a
    no-op. Caller commits; the endpoint acks regardless.
    """
    order = await _lock_order(db, event.out_trade_no)
    if order is None:
        logger.warning("huifu notify: unknown out_trade_no=%s", event.out_trade_no)
        return
    if order.status == PaymentOrderStatus.PAID:
        return  # already granted — idempotent no-op
    if not event.succeeded:
        if order.status == PaymentOrderStatus.PENDING:
            order.status = PaymentOrderStatus.FAILED
            order.reference = {**order.reference, "fail_notify": _excerpt(event.raw)}
        return
    if event.amount_fen != order.amount_fen:
        logger.error(
            "huifu notify amount mismatch order=%s expected_fen=%s got_fen=%s",
            order.id, order.amount_fen, event.amount_fen,
        )
        order.status = PaymentOrderStatus.FAILED
        order.reference = {
            **order.reference,
            "amount_mismatch": {"expected_fen": order.amount_fen, "got_fen": event.amount_fen},
        }
        return
    await _grant_paid(db, order, event)


async def reconcile_paid(db: AsyncSession, order: PaymentOrder, event: VerifiedEvent) -> None:
    """Settle a PENDING order the reconciler found already paid. Same guards
    as :func:`handle_webhook`'s success branch."""
    if order.status == PaymentOrderStatus.PAID:
        return
    if event.amount_fen != order.amount_fen:
        logger.error(
            "huifu reconcile amount mismatch order=%s expected_fen=%s got_fen=%s",
            order.id, order.amount_fen, event.amount_fen,
        )
        order.status = PaymentOrderStatus.FAILED
        return
    await _grant_paid(db, order, event)


async def _lock_order(db: AsyncSession, out_trade_no: str) -> PaymentOrder | None:
    """``SELECT ... FOR UPDATE`` an order by its provider-facing id."""
    return await db.scalar(
        select(PaymentOrder)
        .where(PaymentOrder.out_trade_no == out_trade_no)
        .with_for_update()
    )


async def _grant_paid(
    db: AsyncSession, order: PaymentOrder, event: VerifiedEvent,
) -> None:
    """Grant the frozen credits and mark the order PAID — exactly once.

    On a duplicate ``idempotency_key`` (a racing reconciler/webhook already
    granted), ``apply_topup`` rolls the transaction back itself and raises
    :class:`TopupAlreadyApplied`; we then re-acquire the order and back-link
    the existing TOPUP row instead of granting again.
    """
    key = _topup_key(order.out_trade_no)
    out_trade_no = order.out_trade_no
    try:
        ledger = await apply_topup(
            db,
            user_id=order.user_id,
            amount_micro=order.credit_micro,
            idempotency_key=key,
            reason=_TOPUP_REASON,
        )
    except TopupAlreadyApplied:
        # apply_topup rolled back this tx; re-acquire the order + the
        # ledger row that the earlier call wrote, then back-link it.
        order = await _lock_order(db, out_trade_no)
        ledger_id = await db.scalar(
            select(CreditLedger.id).where(CreditLedger.idempotency_key == key)
        )
        if order is not None and order.status != PaymentOrderStatus.PAID:
            _mark_paid(order, event, ledger_id)
        return
    _mark_paid(order, event, ledger.id)


def _mark_paid(order: PaymentOrder, event: VerifiedEvent, ledger_id: int | None) -> None:
    order.status = PaymentOrderStatus.PAID
    if event.provider_txn_id:
        order.provider_txn_id = event.provider_txn_id
    order.ledger_id = ledger_id
    order.reference = {**order.reference, "paid_event": _excerpt(event.raw)}
