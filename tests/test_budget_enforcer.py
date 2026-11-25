"""Budget enforcer — the stop decision and the pricing/grouping read.

``select_turn_to_stop`` is the money-critical core: it decides whether a
user's in-flight turns get to keep running. Exercised as a pure function.
``_priced_turns_by_user`` is covered with a fake session to pin the
row → priced-turn mapping (frozen-snapshot pricing, per-user grouping).

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.workers.budget_enforcer import (
    RunningTurn,
    _priced_turns_by_user,
    select_turn_to_stop,
)

_SID = uuid.uuid4()


def _turn(reserved: int, priced: int, session_id=None) -> RunningTurn:
    return RunningTurn(
        session_id=session_id or uuid.uuid4(),
        reserved=reserved,
        priced=priced,
    )


# ── select_turn_to_stop ──────────────────────────────────────────────────────


def test_solvent_user_is_never_touched() -> None:
    """An expensive turn a wealthy user can afford keeps running — the
    projection (balance minus overruns) stays above the buffer."""
    turns = [_turn(reserved=100, priced=5_000)]

    assert select_turn_to_stop(turns, balance_micro=10_000, buffer_micro=0) is None


def test_overrun_beyond_balance_stops_the_turn() -> None:
    turns = [_turn(reserved=100, priced=5_000, session_id=_SID)]

    pick = select_turn_to_stop(turns, balance_micro=4_000, buffer_micro=0)

    assert pick is not None and pick.session_id == _SID


def test_worst_overrunner_is_picked_among_concurrent_turns() -> None:
    """Concurrent turns draw on one balance; the aggregate projection trips
    and the biggest overrun is stopped first (gradual — next tick
    re-evaluates the rest)."""
    worst = _turn(reserved=100, priced=9_000, session_id=_SID)
    turns = [_turn(reserved=100, priced=2_000), worst, _turn(reserved=100, priced=500)]

    pick = select_turn_to_stop(turns, balance_micro=5_000, buffer_micro=0)

    assert pick is not None and pick.session_id == _SID


def test_under_hold_turns_offset_the_projection() -> None:
    """A turn running under its hold contributes surplus (signed sum, not
    max(0)): the same overrun that would trip alone survives when a
    sibling's unused hold covers it."""
    over = _turn(reserved=100, priced=200)  # overrun +100
    under = _turn(reserved=100, priced=20)  # surplus  -80

    # projected = 30 - (100 - 80) = 10 > 0 → keep running
    assert select_turn_to_stop([over, under], balance_micro=30, buffer_micro=0) is None
    # projected = 10 - 20 = -10 ≤ 0 → stop the overrunner
    pick = select_turn_to_stop([over, under], balance_micro=10, buffer_micro=0)
    assert pick is not None and pick.session_id == over.session_id


def test_insolvent_user_with_only_under_hold_turns_is_left_alone() -> None:
    """Already-negative balance but every running turn inside its hold:
    those turns are pre-paid, stopping one recovers nothing — the reserve
    gate is what blocks new turns."""
    turns = [_turn(reserved=1_000, priced=300)]

    assert select_turn_to_stop(turns, balance_micro=-500, buffer_micro=0) is None


def test_buffer_raises_the_stop_line() -> None:
    turns = [_turn(reserved=100, priced=1_000)]  # overrun 900

    # projected = 1_000 - 900 = 100; buffer 100 → not above → stop.
    assert select_turn_to_stop(turns, balance_micro=1_000, buffer_micro=100) is not None
    # buffer 0 → 100 > 0 → keep running.
    assert select_turn_to_stop(turns, balance_micro=1_000, buffer_micro=0) is None


# ── _priced_turns_by_user ────────────────────────────────────────────────────


class _Row:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _Db:
    def __init__(self, rows) -> None:
        self._rows = rows

    async def execute(self, _stmt):
        class _Result:
            def __init__(self, rows) -> None:
                self._rows = rows

            def all(self):
                return self._rows

        return _Result(self._rows)


_SNAPSHOT = {
    "model_id": "m",
    "in_per_1k_usd": 0.001,
    "out_per_1k_usd": 0.005,
    "token_markup": 1.0,
    "model_fee_credits": 0.0,
    "per_second_credits": 0.0,  # deterministic: no elapsed-time term
    "reserve_max_output_tokens": 8192,
    "captured_at": "2026-07-01T00:00:00+00:00",
}


async def test_priced_turns_grouped_per_user_with_frozen_snapshot() -> None:
    created_at = datetime.now(UTC) - timedelta(seconds=30)
    sid_a, sid_b = uuid.uuid4(), uuid.uuid4()
    db = _Db(
        [
            _Row(
                user_id=1,
                session_id=sid_a,
                reserved=500,
                rate_snapshot=_SNAPSHOT,
                interim_input_tokens=1000,
                interim_output_tokens=200,
                created_at=created_at,
            ),
            _Row(
                user_id=1,
                session_id=sid_b,
                reserved=500,
                rate_snapshot=_SNAPSHOT,
                interim_input_tokens=2000,
                interim_output_tokens=0,
                created_at=created_at,
            ),
            _Row(
                user_id=2,
                session_id=uuid.uuid4(),
                reserved=500,
                rate_snapshot=_SNAPSHOT,
                interim_input_tokens=0,
                interim_output_tokens=0,
                created_at=created_at,
            ),
        ]
    )

    by_user = await _priced_turns_by_user(db)

    assert set(by_user) == {1, 2}
    assert [turn.session_id for turn in by_user[1]] == [sid_a, sid_b]
    # (1000/1k · 0.001 + 200/1k · 0.005) · 1.0 · 1e9 = 2_000_000 µ-credits —
    # the same cost_micro_credits settle uses, fed from the frozen snapshot.
    assert by_user[1][0].priced == 2_000_000
    assert by_user[1][0].overrun == 2_000_000 - 500
    assert by_user[2][0].priced == 0
