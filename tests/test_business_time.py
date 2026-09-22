"""What "today" means — shared by check-in, credit reporting and revenue.

These used to live with the credit analytics; they moved here when the
window arithmetic became shared, because the property they pin belongs
to the definition of a business day and not to any one consumer.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.common import business_time
from config import settings


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.checkin_timezone)


def test_the_business_day_is_not_utc() -> None:
    # The whole point: a day boundary users and operators recognise.
    assert business_time.business_tz() == _tz()
    assert settings.checkin_timezone != "UTC"


def test_today_is_the_local_date() -> None:
    assert business_time.business_today() == datetime.now(_tz()).date()


def test_window_starts_at_a_business_day_boundary() -> None:
    start = business_time.window_start(7)
    local = start.astimezone(_tz())

    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert start.tzinfo is UTC


def test_window_spans_whole_days_including_today() -> None:
    # A 7-day window is six previous days plus today, not a rolling 168
    # hours that would cut the earliest day in half.
    start = business_time.window_start(7).astimezone(_tz()).date()

    assert business_time.business_today() - start == timedelta(days=6)


def test_a_longer_window_starts_earlier() -> None:
    assert business_time.window_start(90) < business_time.window_start(30)
    assert business_time.window_start(30) < business_time.window_start(7)


def test_single_day_window_starts_today() -> None:
    start = business_time.window_start(1).astimezone(_tz()).date()

    assert start == business_time.business_today()


def test_business_days_is_the_contiguous_axis_of_the_window() -> None:
    days = business_time.business_days(30)

    assert len(days) == 30
    assert days == sorted(days), "oldest first"
    assert days[-1] == business_time.business_today()
    # Contiguous: a chart gap must mean a quiet day, never a missing one.
    assert all(later - earlier == timedelta(days=1) for earlier, later in zip(days, days[1:]))


def test_the_axis_matches_the_window_it_is_drawn_for() -> None:
    # The series and the aggregate must cover the same span, or the bars
    # would not sum to the total they sit under.
    days = business_time.business_days(7)
    start = business_time.window_start(7).astimezone(_tz()).date()

    assert days[0] == start
