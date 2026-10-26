"""Offline unit tests for the reconciler's per-order decision — what the
provider's verdict, non-verdict, or silence does to a PENDING order. The
DB session and the provider are monkeypatched; the settle path
(``reconcile_paid`` / ``apply_topup``) is tested with the webhook."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.errors import PaymentProviderError
from app.models.payment_order import PaymentOrder, PaymentOrderStatus
from app.services.payment import orders, reconcile
from app.services.payment.provider import ChargeState, VerifiedEvent


def _order(expires_at: datetime) -> PaymentOrder:
    return PaymentOrder(
        id=uuid.uuid4(),
        user_id=1,
        out_trade_no="hf1",
        amount_fen=10000,
        credit_micro=1000 * 10_000_000,
        status=PaymentOrderStatus.PENDING,
        reference={"req_date": "20260616"},
        expires_at=expires_at,
    )


def _db(order: PaymentOrder) -> MagicMock:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=order)
    db.commit = AsyncMock()
    return db


def _provider(monkeypatch, answer: VerifiedEvent | Exception) -> None:
    query = (
        AsyncMock(side_effect=answer)
        if isinstance(answer, Exception)
        else AsyncMock(return_value=answer)
    )
    monkeypatch.setattr(orders, "get_provider", lambda: MagicMock(query_charge=query))


def _event(state: ChargeState) -> VerifiedEvent:
    return VerifiedEvent(
        out_trade_no="hf1", provider_txn_id=None, amount_fen=0, state=state, raw={"s": state}
    )


def _past(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


@pytest.fixture
def processing(monkeypatch) -> None:
    _provider(monkeypatch, _event(ChargeState.PROCESSING))


@pytest.fixture
def unreachable(monkeypatch) -> None:
    _provider(monkeypatch, PaymentProviderError("Huifu query failed (status=502)"))


async def test_every_attempt_is_stamped_and_committed(processing) -> None:
    """The stamp is what lets the poller round-robin the PENDING set, so it
    lands even when nothing else about the order changes."""
    order = _order(expires_at=datetime.now(UTC) + timedelta(minutes=5))
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is False

    assert order.status == PaymentOrderStatus.PENDING
    assert order.reconcile_attempted_at is not None
    db.commit.assert_awaited_once()


async def test_failed_verdict_fails_the_order_at_once(monkeypatch) -> None:
    """Mirrors the webhook: FAILED is a verdict, so there is no window to
    wait out — the order is dead whether or not its TTL has passed."""
    _provider(monkeypatch, _event(ChargeState.FAILED))
    order = _order(expires_at=datetime.now(UTC) + timedelta(minutes=5))
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is True

    assert order.status == PaymentOrderStatus.FAILED
    assert "fail_query" in order.reference
    db.commit.assert_awaited_once()


async def test_processing_past_ttl_waits_out_the_settling_window(processing) -> None:
    """The finding: "processing" is not "unpaid". A payment authorised before
    the deadline may still be settling, so the order keeps being asked
    about — expiring it here would take it out of reconciliation while its
    success notification may still be lost in transit."""
    order = _order(expires_at=_past(orders._SETTLING_SECONDS - 60))
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is False

    assert order.status == PaymentOrderStatus.PENDING
    db.commit.assert_awaited_once()  # the stamp


async def test_processing_past_the_settling_window_expires(processing) -> None:
    order = _order(expires_at=_past(orders._SETTLING_SECONDS + 5))
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is True

    assert order.status == PaymentOrderStatus.EXPIRED
    assert order.reference["expired"]["provider"] == "processing"
    db.commit.assert_awaited_once()


async def test_no_answer_past_the_settling_window_still_waits(unreachable) -> None:
    """Silence is weaker than "processing": the order may be paid behind an
    outage, so it gets the long window, not the settling one."""
    order = _order(expires_at=_past(orders._SETTLING_SECONDS + 5))
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is False

    assert order.status == PaymentOrderStatus.PENDING
    db.commit.assert_awaited_once()  # the stamp


async def test_no_answer_past_the_outage_window_expires_flagged(unreachable) -> None:
    order = _order(expires_at=_past(orders._UNANSWERED_SECONDS + 5))
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is True

    assert order.status == PaymentOrderStatus.EXPIRED
    assert order.reference["expired"]["provider"] == "unanswered"
    db.commit.assert_awaited_once()


async def test_settled_order_is_not_touched(processing) -> None:
    order = _order(expires_at=_past(orders._UNANSWERED_SECONDS + 5))
    order.status = PaymentOrderStatus.PAID
    db = _db(order)

    assert await reconcile.reconcile_order(db, order.id) is False

    assert order.status == PaymentOrderStatus.PAID
    assert order.reconcile_attempted_at is None
    db.commit.assert_not_awaited()


async def test_the_provider_is_asked_before_the_row_is_locked(monkeypatch) -> None:
    """A lock held across the network round trip would block this order's
    webhook for as long as the provider takes to answer."""
    order = _order(expires_at=datetime.now(UTC) + timedelta(minutes=5))
    calls: list[str] = []

    async def scalar(statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        calls.append("lock" if "FOR UPDATE" in sql else "read")
        return order

    async def query_charge(**_kwargs):
        calls.append("query")
        return _event(ChargeState.PROCESSING)

    monkeypatch.setattr(orders, "get_provider", lambda: MagicMock(query_charge=query_charge))
    db = _db(order)
    db.scalar = scalar

    await reconcile.reconcile_order(db, order.id)

    assert calls == ["read", "query", "lock"]
