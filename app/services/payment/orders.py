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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.pagination import paginate
from app.errors import OrderNotFoundError, PaymentAmountRangeError
from app.models.credit_ledger import CreditLedger
from app.models.payment_order import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentProvider,
)
from app.services.credits import TopupAlreadyApplied, apply_topup
from app.services.credits.rates import (
    credits_from_cny,
    micro_from_credits,
    recharge_bonus_credits,
)
from app.services.payment.huifu import HuifuGateway
from app.services.payment.provider import Channel, ChargeState, PaymentGateway, VerifiedEvent
from config import settings

logger = logging.getLogger(__name__)

# Beijing time — Huifu's ``req_date`` (yyyyMMdd) is a Beijing calendar day;
# it must match between create and the later reconciliation query.
_CN_TZ = ZoneInfo("Asia/Shanghai")

# One process-wide provider instance (stateless; the SDK client it wraps is
# a process-global initialised lazily on first call).
_provider: PaymentGateway = HuifuGateway()

_TOPUP_REASON = "recharge"

# The provider can only ever tell the reconciler one of three things about
# an order that is not paid: FAILED (a verdict — the order is dead), PROCESSING
# (no verdict — the customer may not have scanned, or the bank is still
# settling a payment they authorised before the deadline), or nothing at all
# (unreachable, a gateway rejection). Neither of the last two is evidence of
# "unpaid", so an order in either keeps being asked about past ``expires_at``
# — for a window sized to the process that could still change its answer —
# and is reaped EXPIRED only when that window closes.
#
# Answered PROCESSING: a settlement takes minutes; after that a P is an order
# nobody paid.
_SETTLING_SECONDS = 15 * 60
# Unanswered: an outage. Long enough to outlast one; bounded so an order the
# provider will never answer for cannot occupy the reconciler forever — only
# PENDING orders are reconciled, so an order expired here is never asked
# about again, and that is why it is flagged and logged (see :func:`expire`).
_UNANSWERED_SECONDS = 24 * 3600


def expiry_due(order: PaymentOrder, now: datetime, *, answered: bool) -> bool:
    """True once an unpaid order is past ``expires_at`` by the window that
    fits what the provider said about it — ``answered`` (PROCESSING) or not
    at all — and may be reaped EXPIRED."""
    window = _SETTLING_SECONDS if answered else _UNANSWERED_SECONDS
    return order.expires_at < now - timedelta(seconds=window)


def expire(order: PaymentOrder, *, now: datetime, answered: bool) -> None:
    """PENDING → EXPIRED. Caller holds the row lock and commits.

    ``reference["expired"]`` records when, and what the provider had said —
    ``processing`` or ``unanswered`` — so an EXPIRED row is never a mystery.
    The unanswered kind is also logged at ERROR: the provider was not
    reachable for the whole window, so this is the one way a paid order
    can end EXPIRED, and support must be able to find these and re-query
    the provider by hand when a customer reports a charge. The webhook
    still resurrects either kind to PAID.
    """
    order.status = PaymentOrderStatus.EXPIRED
    provider = "processing" if answered else "unanswered"
    order.reference = {
        **order.reference,
        "expired": {"at": now.isoformat(), "provider": provider},
    }
    if not answered:
        logger.error(
            "payment order %s (out_trade_no=%s) expired without a provider answer",
            order.id,
            order.out_trade_no,
        )


def fail(order: PaymentOrder, event: VerifiedEvent, *, via: Literal["notify", "query"]) -> None:
    """PENDING → FAILED on the provider's FAILED *verdict*, keeping an
    excerpt of the payload under ``reference["fail_<via>"]`` for audit.
    Caller holds the row lock and commits. A payment that arrives for the
    wrong amount is a different failure with its own record — see
    :func:`_fail_amount_mismatch`."""
    order.status = PaymentOrderStatus.FAILED
    order.reference = {**order.reference, f"fail_{via}": _excerpt(event.raw)}


def _fail_amount_mismatch(
    order: PaymentOrder,
    event: VerifiedEvent,
    *,
    via: Literal["notify", "query"],
) -> None:
    """PENDING → FAILED because the provider reports a different amount than
    the order froze — a tampered callback, or a genuine mismatch worth a
    human. Both settle paths record the two figures, so the row says why it
    failed instead of only that it did."""
    logger.error(
        "huifu %s amount mismatch order=%s expected_fen=%s got_fen=%s",
        via,
        order.id,
        order.amount_fen,
        event.amount_fen,
    )
    order.status = PaymentOrderStatus.FAILED
    order.reference = {
        **order.reference,
        "amount_mismatch": {"expected_fen": order.amount_fen, "got_fen": event.amount_fen},
    }


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
        "resp_code",
        "resp_desc",
        "sub_resp_code",
        "sub_resp_desc",
        "hf_seq_id",
        "trans_stat",
        "trans_amt",
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
    # Freeze paid base + promo bonus onto the order. ``credit_micro`` is the
    # single grant ``apply_topup`` posts on PAID; the base/bonus split is
    # kept in ``reference`` for audit + the response's "incl. bonus" line.
    base_credits = credits_from_cny(amount_cny)
    bonus_credits = recharge_bonus_credits(base_credits)
    credit_micro = micro_from_credits(base_credits + bonus_credits)
    out_trade_no = "hf" + uuid.uuid4().hex
    # Huifu wants Beijing-time strings derived from the same instant as our
    # ``expires_at``: ``req_date`` is the calendar day, ``time_expire`` the
    # QR deadline — so Huifu stops accepting payment exactly when the order
    # expires (no "paid after local expiry" window).
    now = datetime.now(UTC)
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
        reference={
            "req_date": req_date,
            "channel": channel,
            "base_credits": base_credits,
            "bonus_credits": bonus_credits,
        },
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
    db: AsyncSession,
    *,
    order_id: uuid.UUID,
    user_id: int,
) -> PaymentOrder:
    """Fetch a caller-owned order. Raises :class:`OrderNotFoundError`
    (folds not-owned into not-found).

    A read and nothing else — it does not expire the order it finds, and
    it returns the stored status unmassaged. Expiry is the reconciler's
    decision, made against the provider's answer; the read path has no
    such answer, and an EXPIRED written on the clock alone would take the
    order out of reconciliation (only PENDING orders are reconciled) while
    a payment whose notification was lost is still out there. A client
    that wants to grey out a dead QR has ``expires_at`` and its own clock
    for that — deriving EXPIRED *here* would also stop it polling, and a
    payment authorised just before the deadline settles just after it.
    """
    order = await db.scalar(
        select(PaymentOrder).where(
            PaymentOrder.id == order_id,
            PaymentOrder.user_id == user_id,
        )
    )
    if order is None:
        raise OrderNotFoundError(f"Order {order_id} not found")
    return order


async def list_paid_orders(
    db: AsyncSession,
    *,
    user_id: int,
    page: int,
    page_size: int,
) -> tuple[list[PaymentOrder], int]:
    """One newest-first page of the user's PAID orders + the total count —
    the recharge / billing history. Abandoned (EXPIRED) and failed orders
    are excluded; this is the list of actual top-ups. Offset pagination via
    :func:`app.common.pagination.paginate`, count + page sharing one filter."""
    filter_clause = and_(
        PaymentOrder.user_id == user_id,
        PaymentOrder.status == PaymentOrderStatus.PAID,
    )
    total = (
        await db.execute(select(func.count(PaymentOrder.id)).where(filter_clause))
    ).scalar() or 0
    base = select(PaymentOrder).where(filter_clause).order_by(PaymentOrder.created_at.desc())
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return list(rows), total


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
    if event.state is ChargeState.FAILED:
        if order.status == PaymentOrderStatus.PENDING:
            fail(order, event, via="notify")
        return
    if event.state is ChargeState.PROCESSING:
        return  # no verdict — nothing to apply
    if event.amount_fen != order.amount_fen:
        _fail_amount_mismatch(order, event, via="notify")
        return
    await _grant_paid(db, order, event)


async def reconcile_paid(db: AsyncSession, order: PaymentOrder, event: VerifiedEvent) -> None:
    """Settle a PENDING order the reconciler found already paid. Same guards
    as :func:`handle_webhook`'s success branch."""
    if order.status == PaymentOrderStatus.PAID:
        return
    if event.amount_fen != order.amount_fen:
        _fail_amount_mismatch(order, event, via="query")
        return
    await _grant_paid(db, order, event)


async def _lock_order(db: AsyncSession, out_trade_no: str) -> PaymentOrder | None:
    """``SELECT ... FOR UPDATE`` an order by its provider-facing id."""
    return await db.scalar(
        select(PaymentOrder).where(PaymentOrder.out_trade_no == out_trade_no).with_for_update()
    )


async def _grant_paid(
    db: AsyncSession,
    order: PaymentOrder,
    event: VerifiedEvent,
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
        # Its own name, not a rebind of ``order``: this runs in a *fresh*
        # transaction — ``apply_topup`` rolled the previous one back — so the
        # row is re-read rather than still held, and the guard below is what
        # says nothing was found. Reassigning would make that guard read as
        # dead code on a parameter the caller declared non-optional.
        relocked = await _lock_order(db, out_trade_no)
        ledger_id = await db.scalar(
            select(CreditLedger.id).where(CreditLedger.idempotency_key == key)
        )
        if relocked is not None and relocked.status != PaymentOrderStatus.PAID:
            _mark_paid(relocked, event, ledger_id)
        return
    _mark_paid(order, event, ledger.id)


def _mark_paid(order: PaymentOrder, event: VerifiedEvent, ledger_id: int | None) -> None:
    order.status = PaymentOrderStatus.PAID
    if event.provider_txn_id:
        order.provider_txn_id = event.provider_txn_id
    order.ledger_id = ledger_id
    order.reference = {**order.reference, "paid_event": _excerpt(event.raw)}
