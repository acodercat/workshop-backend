"""Offline unit test for the order-poll endpoint.

Calls the handler coroutine directly with a monkeypatched service (no app
boot, no DB), pinning the two things the read path must not do: massage the
status, and write.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import app.api.public.payment as ep
from app.models.payment_order import PaymentOrder, PaymentOrderStatus


def _order(expires_at: datetime) -> PaymentOrder:
    return PaymentOrder(
        id=uuid.uuid4(),
        user_id=1,
        out_trade_no="hf1",
        amount_fen=10000,
        credit_micro=1000 * 10_000_000,
        status=PaymentOrderStatus.PENDING,
        reference={},
        expires_at=expires_at,
    )


async def test_poll_returns_the_stored_status_and_writes_nothing(monkeypatch) -> None:
    """An unpaid order past its TTL still reads PENDING. The client polls
    "until it stops being PENDING", so deriving EXPIRED here would stop it
    polling at the deadline — and a payment authorised just before it
    settles just after. Only the reconciler moves the order, on the
    provider's answer.
    """
    order = _order(expires_at=datetime.now(UTC) - timedelta(hours=1))
    monkeypatch.setattr(ep.payment, "get_order", AsyncMock(return_value=order))
    db = MagicMock()
    db.commit = AsyncMock()

    resp = await ep.get_payment_order(order.id, db, MagicMock(id=1))

    assert resp.status == PaymentOrderStatus.PENDING.value
    assert resp.expires_at == order.expires_at
    db.commit.assert_not_awaited()
