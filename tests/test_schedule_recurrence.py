"""Recurrence math for scheduled tasks.

Pure functions over an unsaved ``Schedule``, so every case here is data:
build a spec, name an instant, assert the instant that comes back. No DB,
no clock, no timezone leaking in from the host.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.schedule import Schedule, ScheduleFrequency
from app.services.schedule import catch_up_grace, next_run_after

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def _schedule(**overrides) -> Schedule:
    """An unsaved schedule; DAILY 09:00 Asia/Shanghai unless overridden."""
    spec = {
        "frequency": ScheduleFrequency.DAILY,
        "at_time": time(9, 0),
        "timezone": "Asia/Shanghai",
        "run_date": None,
        "weekday": None,
        "day_of_month": None,
    }
    return Schedule(**{**spec, **overrides})


def _local(dt: datetime, tz: ZoneInfo = SHANGHAI) -> datetime:
    return dt.astimezone(tz)


class TestDaily:
    def test_fires_later_the_same_day(self):
        after = datetime(2026, 9, 10, 7, 0, tzinfo=SHANGHAI)
        assert next_run_after(_schedule(), after) == datetime(2026, 9, 10, 1, 0, tzinfo=UTC)

    def test_rolls_to_tomorrow_once_the_time_has_passed(self):
        after = datetime(2026, 9, 10, 9, 0, 1, tzinfo=SHANGHAI)
        assert _local(next_run_after(_schedule(), after)) == datetime(
            2026, 9, 11, 9, 0, tzinfo=SHANGHAI
        )

    def test_exactly_on_the_hour_is_not_now_but_next(self):
        # ``strictly after`` matters: returning the instant we were handed
        # would re-fire the occurrence the dispatcher just claimed.
        after = datetime(2026, 9, 10, 9, 0, tzinfo=SHANGHAI)
        assert _local(next_run_after(_schedule(), after)).date() == date(2026, 9, 11)


class TestWeekly:
    @pytest.mark.parametrize("weekday", range(7))
    def test_lands_on_the_configured_weekday(self, weekday):
        schedule = _schedule(frequency=ScheduleFrequency.WEEKLY, weekday=weekday)
        result = _local(next_run_after(schedule, datetime(2026, 9, 10, 7, 0, tzinfo=SHANGHAI)))
        assert result.weekday() == weekday
        assert result.time() == time(9, 0)

    def test_today_already_past_rolls_a_full_week(self):
        today = datetime(2026, 9, 10, 12, 0, tzinfo=SHANGHAI)
        schedule = _schedule(frequency=ScheduleFrequency.WEEKLY, weekday=today.weekday())
        assert _local(next_run_after(schedule, today)).date() == today.date() + timedelta(days=7)


class TestMonthly:
    def test_fires_later_this_month(self):
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=20)
        result = _local(next_run_after(schedule, datetime(2026, 9, 10, 7, 0, tzinfo=SHANGHAI)))
        assert result.date() == date(2026, 9, 20)

    def test_rolls_into_next_month_once_the_day_has_passed(self):
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=5)
        result = _local(next_run_after(schedule, datetime(2026, 9, 10, 7, 0, tzinfo=SHANGHAI)))
        assert result.date() == date(2026, 10, 5)

    def test_december_rolls_into_january(self):
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=5)
        result = _local(next_run_after(schedule, datetime(2026, 12, 10, 7, 0, tzinfo=SHANGHAI)))
        assert result.date() == date(2027, 1, 5)

    def test_day_28_exists_in_every_month(self):
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=28)
        result = _local(next_run_after(schedule, datetime(2027, 2, 1, 7, 0, tzinfo=SHANGHAI)))
        assert result.date() == date(2027, 2, 28)

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (29, date(2027, 3, 29)),  # 2027 is not a leap year
            (30, date(2027, 3, 30)),
            (31, date(2027, 3, 31)),
        ],
    )
    def test_a_month_too_short_is_skipped(self, day, expected):
        # The cron reading of "the 31st", and the one a user picking it is
        # asking for. February has none of these days in 2027, so the next
        # occurrence is in March — not clamped to the 28th, which would be a
        # different feature.
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=day)
        result = _local(next_run_after(schedule, datetime(2027, 2, 1, 7, 0, tzinfo=SHANGHAI)))
        assert result.date() == expected

    def test_the_29th_fires_in_a_leap_february(self):
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=29)
        result = _local(next_run_after(schedule, datetime(2028, 2, 1, 7, 0, tzinfo=SHANGHAI)))
        assert result.date() == date(2028, 2, 29)

    def test_the_31st_fires_only_in_long_months(self):
        # Seven months a year, and the sequence steps over each short one
        # rather than stalling on it.
        schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=31)
        after = datetime(2026, 1, 1, tzinfo=SHANGHAI)
        months = []
        for _ in range(7):
            after = next_run_after(schedule, after)
            months.append(_local(after).month)
        assert months == [1, 3, 5, 7, 8, 10, 12]


class TestOnce:
    def test_returns_the_instant_while_it_is_still_ahead(self):
        schedule = _schedule(frequency=ScheduleFrequency.ONCE, run_date=date(2026, 9, 20))
        after = datetime(2026, 9, 10, 7, 0, tzinfo=SHANGHAI)
        assert _local(next_run_after(schedule, after)) == datetime(
            2026, 9, 20, 9, 0, tzinfo=SHANGHAI
        )

    def test_returns_none_once_spent(self):
        # None is what the dispatcher turns into status=COMPLETED.
        schedule = _schedule(frequency=ScheduleFrequency.ONCE, run_date=date(2026, 9, 10))
        after = datetime(2026, 9, 10, 9, 0, tzinfo=SHANGHAI)
        assert next_run_after(schedule, after) is None


class TestTimezone:
    def test_result_is_utc_normalised(self):
        result = next_run_after(_schedule(), datetime(2026, 9, 10, 7, 0, tzinfo=SHANGHAI))
        assert result.utcoffset() == timedelta(0)

    def test_wall_clock_survives_a_dst_transition(self):
        # 2026-03-08 is US spring-forward. A 09:00 schedule must stay 09:00
        # local on both sides — the offset moves, the wall clock does not.
        schedule = _schedule(timezone="America/New_York")
        before = next_run_after(schedule, datetime(2026, 3, 6, 10, 0, tzinfo=NEW_YORK))
        after = next_run_after(schedule, datetime(2026, 3, 9, 10, 0, tzinfo=NEW_YORK))
        assert _local(before, NEW_YORK).hour == 9
        assert _local(after, NEW_YORK).hour == 9
        # Same wall clock, different offset: EST -05:00 before, EDT -04:00
        # after. A stored UTC offset would have kept the offset and moved
        # the wall clock, which is the bug this schema shape avoids.
        assert _local(before, NEW_YORK).utcoffset() == timedelta(hours=-5)
        assert _local(after, NEW_YORK).utcoffset() == timedelta(hours=-4)

    def test_a_local_time_dst_skips_still_produces_a_run(self):
        # 02:30 does not exist on 2026-03-08 in New York. Resolving forward
        # beats raising inside a background process at 02:30.
        schedule = _schedule(timezone="America/New_York", at_time=time(2, 30))
        result = next_run_after(schedule, datetime(2026, 3, 7, 10, 0, tzinfo=NEW_YORK))
        assert result is not None
        assert _local(result, NEW_YORK).date() == date(2026, 3, 8)


class TestCatchUp:
    def test_an_outage_advances_one_step_not_once_per_missed_occurrence(self):
        # Six days down. Advancing with ``now()`` lands on the next future
        # occurrence directly — the user gets one report back, not six.
        schedule = _schedule()
        now = datetime(2026, 9, 10, 12, 0, tzinfo=SHANGHAI)
        assert _local(next_run_after(schedule, now)).date() == date(2026, 9, 11)

    @pytest.mark.parametrize(
        ("frequency", "expected"),
        [
            (ScheduleFrequency.ONCE, timedelta(minutes=15)),
            (ScheduleFrequency.DAILY, timedelta(hours=2)),
            (ScheduleFrequency.WEEKLY, timedelta(hours=2)),
            (ScheduleFrequency.MONTHLY, timedelta(hours=2)),
        ],
    )
    def test_grace_window(self, frequency, expected):
        assert catch_up_grace(frequency) == expected

    def test_a_one_shots_grace_outlasts_every_gap_the_dispatcher_can_leave(self):
        """The grace has to cover the dispatcher's own worst-case silence.

        A one-shot is picked up on the first tick at or after its instant, so
        the grace must exceed the largest gap between two ticks — otherwise
        the process writes off the very occurrences its own scheduling
        delayed, retiring them COMPLETED having never run once. Two
        constants set that gap: the healthy interval, and the failure
        backoff's ceiling (which climbs on exactly the database trouble
        catch-up exists for).

        Pinned here because nothing else can be: these three numbers live in
        two modules and none of them is a validated setting any more.
        """
        from app.workers.schedule_dispatcher import (
            _MAX_BACKOFF_SECONDS,
            _TICK_SECONDS,
        )

        grace = catch_up_grace(ScheduleFrequency.ONCE)
        assert grace > timedelta(seconds=_TICK_SECONDS)
        assert grace > timedelta(seconds=_MAX_BACKOFF_SECONDS)

    def test_the_backoff_ceiling_is_not_below_the_healthy_interval(self):
        # ``_backoff_delay`` clamps with ``max(interval, min(...))`` so this
        # cannot invert the backoff even if it were, but a ceiling under the
        # interval would make the clamp the only thing doing any work — a
        # confusing pair of constants rather than a broken one.
        from app.workers.schedule_dispatcher import (
            _MAX_BACKOFF_SECONDS,
            _TICK_SECONDS,
        )

        assert _MAX_BACKOFF_SECONDS >= _TICK_SECONDS


def test_naive_after_is_rejected():
    # A naive instant here would silently be read as the host's zone, and
    # the host is not the user.
    with pytest.raises(ValueError, match="aware"):
        next_run_after(_schedule(), datetime(2026, 9, 10, 7, 0))


class TestMonthlyExhaustive:
    """The monthly contract over every day and every month, checked not argued.

    What this sweep does catch: a returned date that does not exist, that is
    not strictly after the input, that landed on the wrong day (a clamp to
    month end rather than a skip), or that lost its ``at_time``.

    What it does **not** catch, and worth knowing before trusting it: the
    bounded loop in ``_first_firing_date`` could be replaced by a single
    "try, else next month" step and every case here would still pass. On the
    Gregorian calendar the two are behaviourally identical, because the
    longest run of months missing a day is one — which is exactly what
    ``test_the_loop_never_needs_more_than_one_step`` measures. The loop earns
    its place by not depending on that fact, not by producing a different
    answer today.
    """

    def test_every_day_of_month_over_six_years(self):
        # 1-31 × every start date 2024-2029 × three times of day. Includes
        # two leap Februaries and every short-month boundary.
        for day in range(1, 32):
            schedule = _schedule(frequency=ScheduleFrequency.MONTHLY, day_of_month=day)
            cursor = date(2024, 1, 1)
            while cursor < date(2030, 1, 1):
                for hour in (0, 9, 23):
                    after = datetime(cursor.year, cursor.month, cursor.day, hour, tzinfo=SHANGHAI)
                    result = next_run_after(schedule, after)
                    assert result is not None, (day, after)
                    local = _local(result)
                    assert local > after, (day, after, local)
                    # The day is exact — never clamped to a month's end.
                    assert local.day == day, (day, after, local)
                    assert local.time() == time(9, 0), (day, after, local)
                cursor += timedelta(days=1)

    def test_the_loop_never_needs_more_than_one_step(self):
        # Where the bound of 12 inside ``_first_firing_date`` gets its slack.
        # The claim has to hold for the 31st, not just for 29-30: the months
        # lacking a 31st are Feb, Apr, Jun, Sep and Nov — pairwise
        # non-adjacent — and December has 31, so the year wrap is safe.
        from calendar import monthrange

        worst = 0
        for day in range(1, 32):
            for year in range(2024, 2101):
                for month in range(1, 13):
                    steps, y, m = 0, year, month
                    while monthrange(y, m)[1] < day:
                        steps += 1
                        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                    worst = max(worst, steps)
        assert worst == 1, f"expected at most one step, measured {worst}"
