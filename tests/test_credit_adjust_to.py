"""``apply_adjust_to`` — set a balance to a target, exactly.

The script's old ``--to`` read the balance unlocked and without settling
first, so a finished-but-unsettled turn or a concurrent write left the
balance off the target it reported having set.
"""

from unittest.mock import AsyncMock

import pytest

from app.services.credits import adjust


def _patch(monkeypatch, *, balance: int) -> AsyncMock:
    monkeypatch.setattr(adjust, "settle_open_for_user", AsyncMock())
    monkeypatch.setattr(adjust, "lock_user_balance", AsyncMock(return_value=balance))
    apply = AsyncMock()
    monkeypatch.setattr(adjust, "apply_adjust", apply)
    return apply


def _db(*, in_flight: bool) -> AsyncMock:
    db = AsyncMock()
    db.scalar.return_value = in_flight
    return db


async def _adjust_to(db, target_micro: int):
    return await adjust.apply_adjust_to(
        db, user_id=7, target_micro=target_micro, idempotency_key="k", reason="r"
    )


async def test_the_delta_is_taken_against_the_settled_locked_balance(monkeypatch) -> None:
    apply = _patch(monkeypatch, balance=30)

    await _adjust_to(_db(in_flight=False), target_micro=100)

    adjust.settle_open_for_user.assert_awaited_once()
    assert apply.await_args.kwargs["amount_micro"] == 70


async def test_a_balance_already_at_the_target_posts_nothing(monkeypatch) -> None:
    apply = _patch(monkeypatch, balance=100)

    assert await _adjust_to(_db(in_flight=False), target_micro=100) is None
    apply.assert_not_awaited()


async def test_a_turn_in_flight_is_refused(monkeypatch) -> None:
    # Its reservation would settle later and move the balance off the target.
    apply = _patch(monkeypatch, balance=30)

    with pytest.raises(adjust.BalanceNotSettled):
        await _adjust_to(_db(in_flight=True), target_micro=0)
    apply.assert_not_awaited()
