"""Offline unit tests for the withdraw / convert gates + balance math.
The DB session and the locked-balance read are monkeypatched; these pin
the disabled / minimum / insufficient guards and the frozen split."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import (
    CommissionAmountRangeError,
    CommissionDisabledError,
    InsufficientCommissionError,
)
from app.services.commission import balance, convert, withdraw
from config import settings


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "commission_enabled", True)
    monkeypatch.setattr(settings, "commission_min_withdraw_fen", 1000)
    monkeypatch.setattr(settings, "commission_min_convert_fen", 100)


async def _request(amount_fen):
    return await withdraw.request_withdrawal(
        MagicMock(), user_id=1, amount_fen=amount_fen,
        payout_account="alice@example.com", payout_name="Alice",
    )


async def test_withdraw_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "commission_enabled", False)
    with pytest.raises(CommissionDisabledError):
        await _request(1000)


async def test_withdraw_below_minimum(enabled) -> None:
    with pytest.raises(CommissionAmountRangeError):
        await _request(500)


async def test_withdraw_insufficient(monkeypatch, enabled) -> None:
    monkeypatch.setattr(withdraw, "lock_withdrawable_fen", AsyncMock(return_value=500))
    with pytest.raises(InsufficientCommissionError):
        await _request(1000)


async def test_convert_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "commission_enabled", False)
    with pytest.raises(CommissionDisabledError):
        await convert.convert_to_credits(MagicMock(), user_id=1, amount_fen=1000)


async def test_convert_below_minimum(enabled) -> None:
    with pytest.raises(CommissionAmountRangeError):
        await convert.convert_to_credits(MagicMock(), user_id=1, amount_fen=50)


async def test_convert_insufficient(monkeypatch, enabled) -> None:
    monkeypatch.setattr(convert, "lock_withdrawable_fen", AsyncMock(return_value=500))
    with pytest.raises(InsufficientCommissionError):
        await convert.convert_to_credits(MagicMock(), user_id=1, amount_fen=10000)


async def test_convert_floors_and_debits_exact(monkeypatch, enabled) -> None:
    # ¥5.55 (555 分) at ¥0.1/credit → 55 credits (¥5.50); the 5-分 remainder
    # must stay in the wallet — debit the floored value, not the requested.
    monkeypatch.setattr(convert, "lock_withdrawable_fen", AsyncMock(return_value=10000))
    entry = MagicMock(id=1, reference={})
    insert = AsyncMock(return_value=entry)
    monkeypatch.setattr(convert, "insert_ledger_and_bump_balance", insert)
    monkeypatch.setattr(convert.credits, "apply_topup", AsyncMock(return_value=MagicMock(id=9)))

    result = await convert.convert_to_credits(MagicMock(), user_id=1, amount_fen=555)

    assert result.granted_credits == 55
    assert result.spent_fen == 550  # not 555 — the 5-分 remainder stays
    assert insert.await_args.kwargs["amount_fen"] == -550


async def test_balance_splits_total_minus_frozen() -> None:
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[1000, 300])  # total, then frozen sum
    bal = await balance.get_balance(db, 1)
    assert bal.total_fen == 1000
    assert bal.frozen_fen == 300
    assert bal.withdrawable_fen == 700
