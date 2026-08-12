"""Offline unit tests for referral-commission accrual — the disabled
gate, referrer lookup, amount + idempotency-key shape, and the
no-referrer / not-paid no-ops. The DB session and the ledger primitive
are monkeypatched; the funnel SQL is covered elsewhere."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services.commission import accrual
from app.services.commission.accrual import _commission_key, apply_commission
from config import settings


def _order(status=PaymentOrderStatus.PAID, amount_fen=10000) -> PaymentOrder:
    order = PaymentOrder(
        user_id=7,
        out_trade_no="hf1",
        amount_fen=amount_fen,
        credit_micro=0,
        status=status,
        reference={},
    )
    order.id = uuid.uuid4()
    return order


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "commission_enabled", True)
    monkeypatch.setattr(settings, "commission_rate", 0.1)
    monkeypatch.setattr(settings, "commission_hold_days", 7)


def test_commission_key_format() -> None:
    assert _commission_key("hf1") == "commission:hf1"


async def test_disabled_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "commission_enabled", False)
    db = MagicMock()
    db.scalar = AsyncMock()
    await accrual.accrue_for_order(db, out_trade_no="hf1")
    db.scalar.assert_not_awaited()


async def test_accrues_rate_of_paid_amount(monkeypatch, enabled) -> None:
    order = _order(amount_fen=10000)
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[order, 42])  # order, then referrer_user_id
    db.commit = AsyncMock()
    applied = AsyncMock()
    monkeypatch.setattr(accrual, "apply_commission", applied)

    await accrual.accrue_for_order(db, out_trade_no="hf1")

    applied.assert_awaited_once()
    kwargs = applied.await_args.kwargs
    assert kwargs["referrer_user_id"] == 42
    assert kwargs["invitee_user_id"] == 7
    assert kwargs["amount_fen"] == 1000  # 10% of 10000
    assert kwargs["out_trade_no"] == "hf1"
    assert kwargs["available_at"] > datetime.now(UTC)
    db.commit.assert_awaited_once()


async def test_no_referrer_skips(monkeypatch, enabled) -> None:
    order = _order()
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[order, None])  # SYSTEM code → no referrer
    applied = AsyncMock()
    monkeypatch.setattr(accrual, "apply_commission", applied)

    await accrual.accrue_for_order(db, out_trade_no="hf1")

    applied.assert_not_awaited()


async def test_not_paid_skips(monkeypatch, enabled) -> None:
    order = _order(status=PaymentOrderStatus.PENDING)
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[order])
    applied = AsyncMock()
    monkeypatch.setattr(accrual, "apply_commission", applied)

    await accrual.accrue_for_order(db, out_trade_no="hf1")

    applied.assert_not_awaited()


async def test_apply_commission_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        await apply_commission(
            MagicMock(),
            referrer_user_id=1,
            invitee_user_id=2,
            payment_order_id=uuid.uuid4(),
            out_trade_no="hf1",
            amount_fen=0,
            available_at=datetime.now(UTC),
        )
