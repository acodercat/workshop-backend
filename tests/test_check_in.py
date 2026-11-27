"""Offline unit tests for the daily check-in streak logic + reward guard.

No DB/network: ``_current_streak`` is a pure function over a ``CheckIn``
row's ``check_in_date`` / ``streak_day``, and the reward guard raises
before touching the session.
"""

from datetime import date, timedelta

import pytest

from app.models.check_in import CheckIn
from app.services.check_in import _current_streak
from app.services.credits.check_in import apply_check_in_reward

_TODAY = date(2026, 7, 1)


def _row(day: date, streak: int) -> CheckIn:
    return CheckIn(check_in_date=day, streak_day=streak)


@pytest.mark.parametrize(
    "latest, expected",
    [
        (None, 0),  # never checked in
        (_row(_TODAY, 5), 5),  # already claimed today → current streak 5
        (_row(_TODAY - timedelta(days=1), 5), 5),  # yesterday → still alive
        (_row(_TODAY - timedelta(days=2), 5), 0),  # 1-day gap → lapsed
        (_row(_TODAY - timedelta(days=30), 9), 0),  # long gap → lapsed
    ],
)
def test_current_streak(latest, expected):
    assert _current_streak(latest, _TODAY) == expected


def test_claim_extends_or_resets_streak():
    # Claiming today = current streak + 1.
    assert _current_streak(_row(_TODAY - timedelta(days=1), 5), _TODAY) + 1 == 6
    # After a gap the streak resets to 1.
    assert _current_streak(_row(_TODAY - timedelta(days=3), 5), _TODAY) + 1 == 1
    # First check-in ever = 1.
    assert _current_streak(None, _TODAY) + 1 == 1


async def test_apply_check_in_reward_rejects_nonpositive():
    # The positivity guard fires before the session is touched, so a
    # ``None`` db is never used.
    with pytest.raises(ValueError):
        await apply_check_in_reward(
            None,
            user_id=1,
            amount_micro=0,
            check_in_date=_TODAY,
            streak_day=1,
        )
