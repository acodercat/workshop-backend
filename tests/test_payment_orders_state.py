"""Offline unit tests for the order state machine — create freeze +
``handle_webhook`` idempotency. The DB session and ``apply_topup`` are
monkeypatched (the credit-funnel + SQL are tested elsewhere); these tests
pin the routing + guard logic."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.errors import OrderNotFoundError, PaymentAmountRangeError
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services.credits import TopupAlreadyApplied
from app.services.payment import orders
from app.services.payment.provider import ChargeResult, ChargeState, VerifiedEvent

_MICRO_1000_CREDITS = 1000 * 10_000_000


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _order(status=PaymentOrderStatus.PENDING, amount_fen=10000) -> PaymentOrder:
    return PaymentOrder(
        user_id=1,
        out_trade_no="hf1",
        amount_fen=amount_fen,
        credit_micro=_MICRO_1000_CREDITS,
        status=status,
        reference={},
    )


def _paid_event(amount_fen=10000) -> VerifiedEvent:
    return VerifiedEvent(
        out_trade_no="hf1",
        provider_txn_id="H9",
        amount_fen=amount_fen,
        state=ChargeState.PAID,
        raw={"sub_resp_code": "00000000"},
    )


async def test_handle_webhook_grants_once_on_success(monkeypatch) -> None:
    order = _order()
    monkeypatch.setattr(orders, "_lock_order", AsyncMock(return_value=order))
    topup = AsyncMock(return_value=MagicMock(id=55))
    monkeypatch.setattr(orders, "apply_topup", topup)

    await orders.handle_webhook(MagicMock(), _paid_event())

    assert order.status == PaymentOrderStatus.PAID
    assert order.ledger_id == 55
    topup.assert_awaited_once()


async def test_handle_webhook_failure_marks_failed_without_topup(monkeypatch) -> None:
    order = _order()
    monkeypatch.setattr(orders, "_lock_order", AsyncMock(return_value=order))
    topup = AsyncMock()
    monkeypatch.setattr(orders, "apply_topup", topup)

    fail_event = VerifiedEvent("hf1", None, 0, state=ChargeState.FAILED, raw={"trans_stat": "F"})
    await orders.handle_webhook(MagicMock(), fail_event)

    assert order.status == PaymentOrderStatus.FAILED
    assert order.reference["fail_notify"] == {"trans_stat": "F"}
    topup.assert_not_awaited()


async def test_handle_webhook_processing_changes_nothing(monkeypatch) -> None:
    # No verdict: the order is neither paid nor dead. It stays PENDING for
    # the reconciler; nothing is granted and nothing is failed.
    order = _order()
    monkeypatch.setattr(orders, "_lock_order", AsyncMock(return_value=order))
    topup = AsyncMock()
    monkeypatch.setattr(orders, "apply_topup", topup)

    await orders.handle_webhook(
        MagicMock(), VerifiedEvent("hf1", None, 0, state=ChargeState.PROCESSING, raw={})
    )

    assert order.status == PaymentOrderStatus.PENDING
    assert order.reference == {}
    topup.assert_not_awaited()


async def test_handle_webhook_amount_mismatch_fails_without_topup(monkeypatch) -> None:
    order = _order(amount_fen=10000)
    monkeypatch.setattr(orders, "_lock_order", AsyncMock(return_value=order))
    topup = AsyncMock()
    monkeypatch.setattr(orders, "apply_topup", topup)

    await orders.handle_webhook(MagicMock(), _paid_event(amount_fen=9999))

    assert order.status == PaymentOrderStatus.FAILED
    topup.assert_not_awaited()


async def test_handle_webhook_already_paid_is_noop(monkeypatch) -> None:
    order = _order(status=PaymentOrderStatus.PAID)
    monkeypatch.setattr(orders, "_lock_order", AsyncMock(return_value=order))
    topup = AsyncMock()
    monkeypatch.setattr(orders, "apply_topup", topup)

    await orders.handle_webhook(MagicMock(), _paid_event())

    assert order.status == PaymentOrderStatus.PAID
    topup.assert_not_awaited()


async def test_handle_webhook_duplicate_topup_backlinks_existing(monkeypatch) -> None:
    # A racing reconciler already granted: apply_topup raises (and rolls the
    # tx back); handle_webhook must re-acquire the order and back-link the
    # existing ledger row instead of double-granting.
    order = _order()
    monkeypatch.setattr(orders, "_lock_order", AsyncMock(return_value=order))
    monkeypatch.setattr(
        orders,
        "apply_topup",
        AsyncMock(side_effect=TopupAlreadyApplied("dup")),
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=77)  # the existing ledger row id

    await orders.handle_webhook(db, _paid_event())

    assert order.status == PaymentOrderStatus.PAID
    assert order.ledger_id == 77


async def test_create_order_freezes_grant_and_calls_provider(monkeypatch) -> None:
    fake_provider = MagicMock()
    fake_provider.create_charge = AsyncMock(
        return_value=ChargeResult(
            out_trade_no="x",
            qr_code="https://qr",
            trade_type="A_NATIVE",
            provider_txn_id="H1",
            raw={},
        )
    )
    monkeypatch.setattr(orders, "_provider", fake_provider)
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()

    order = await orders.create_order(
        db,
        user_id=1,
        amount_cny=100,
        channel="ALIPAY",
    )

    assert order.amount_fen == 10000
    # ¥100 → 1000 paid base + 10% promo bonus (default credit_bonus_rate)
    # = 1100 credits, frozen on the order; the split is kept in reference.
    assert order.credit_micro == 1100 * 10_000_000
    assert order.reference["base_credits"] == 1000
    assert order.reference["bonus_credits"] == 100
    assert order.reference["qr_code"] == "https://qr"
    assert order.status == PaymentOrderStatus.PENDING
    fake_provider.create_charge.assert_awaited_once()


async def test_create_order_rejects_amount_below_minimum() -> None:
    with pytest.raises(PaymentAmountRangeError):
        await orders.create_order(
            MagicMock(),
            user_id=1,
            amount_cny=0.5,
            channel="ALIPAY",
        )


def _reapable_expires_at() -> datetime:
    """An ``expires_at`` past every reconciler window — reapable now."""
    return datetime.now(UTC) - timedelta(seconds=orders._UNANSWERED_SECONDS + 5)


async def test_get_order_is_a_read() -> None:
    # No lock, no write: the poll path never expires an order, however far
    # past its TTL — EXPIRED is the reconciler's decision, made against the
    # provider's answer. A clock-only EXPIRED here would take the order out
    # of reconciliation while a payment whose notification was lost is
    # still out there.
    order = _order()
    order.expires_at = _reapable_expires_at()
    captured: dict = {}

    async def _scalar(stmt):
        captured["stmt"] = stmt
        return order

    db = MagicMock()
    db.scalar = _scalar

    result = await orders.get_order(db, order_id=uuid.uuid4(), user_id=1)

    assert result.status == PaymentOrderStatus.PENDING
    assert "FOR UPDATE" not in _sql(captured["stmt"])


def test_amount_mismatch_records_both_figures_on_either_settle_path() -> None:
    """A FAILED row must say why. Both settle paths reach the same writer,
    so the reconciler's mismatch is no longer the one FAILED with an empty
    ``reference``."""
    for via in ("notify", "query"):
        order = _order(amount_fen=10000)
        orders._fail_amount_mismatch(order, _paid_event(amount_fen=9900), via=via)
        assert order.status == PaymentOrderStatus.FAILED
        assert order.reference["amount_mismatch"] == {"expected_fen": 10000, "got_fen": 9900}


async def test_get_order_missing_raises_not_found() -> None:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)

    with pytest.raises(OrderNotFoundError):
        await orders.get_order(db, order_id=uuid.uuid4(), user_id=1)


def test_expiry_window_depends_on_what_the_provider_said() -> None:
    now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
    order = _order()
    # Just past expires_at: neither kind is due — a payment authorised
    # before the deadline may still be settling.
    order.expires_at = now - timedelta(seconds=10)
    assert orders.expiry_due(order, now, answered=True) is False
    assert orders.expiry_due(order, now, answered=False) is False
    # Past the settling window: an answered "processing" is an order nobody
    # paid; an unanswered one may still be a paid order behind an outage.
    order.expires_at = now - timedelta(seconds=orders._SETTLING_SECONDS + 1)
    assert orders.expiry_due(order, now, answered=True) is True
    assert orders.expiry_due(order, now, answered=False) is False
    # Past the outage window: reap even the unanswered one.
    order.expires_at = now - timedelta(seconds=orders._UNANSWERED_SECONDS + 1)
    assert orders.expiry_due(order, now, answered=False) is True


def test_expire_records_what_was_known_and_logs_only_the_unanswered_kind(caplog) -> None:
    now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
    answered = _order()
    with caplog.at_level("ERROR", logger="app.services.payment.orders"):
        orders.expire(answered, now=now, answered=True)
    assert answered.status == PaymentOrderStatus.EXPIRED
    assert answered.reference["expired"] == {"at": now.isoformat(), "provider": "processing"}
    assert caplog.text == ""

    unanswered = _order()
    with caplog.at_level("ERROR", logger="app.services.payment.orders"):
        orders.expire(unanswered, now=now, answered=False)
    assert unanswered.status == PaymentOrderStatus.EXPIRED
    assert unanswered.reference["expired"] == {"at": now.isoformat(), "provider": "unanswered"}
    assert "without a provider answer" in caplog.text
