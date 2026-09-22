"""Recurrence math for scheduled tasks — pure functions, no I/O.

Two questions, both answered here and nowhere else: *when does this
schedule fire next*, and *how late may an occurrence be and still run*.

**Timezones are per-schedule, not per-platform.** This is the one module
that deliberately does not go through ``app/common/business_time.py``. That
module cuts the platform's business day in a single configured zone so
reporting agrees with itself; a schedule instead fires in the zone its owner
chose, snapshotted on the row. "09:00" means 09:00 where the user is, which
is why the recurrence is computed on local wall-clock dates and only
converted to UTC at the end — a stored UTC offset would drift by an hour
twice a year for any owner in a DST zone.
"""

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.schedule import Schedule, ScheduleFrequency

# How late an occurrence may be and still fire rather than be written off as
# ``too_late``. Lateness happens when the background process was down, the
# database was unreachable, or a long batch pushed the tick late.
#
# Two hours for a recurring schedule: a 09:00 daily report is still worth
# delivering at 10:30 — refusing to would cost the user that day's report
# over an outage that was already fixed.
#
# A one-shot gets a tighter window, because its owner picked an instant and
# one arriving hours later is worse than one that says it was missed. But
# "tighter" has a hard floor: it must outlast the longest gap the dispatcher
# can leave between ticks, or the process's own failure backoff would write
# off the one-shots it was moments from delivering. That backoff caps at ten
# minutes (``schedule_dispatcher._MAX_BACKOFF_SECONDS``) and a saturated
# batch queue can add a few more, so fifteen. The relationship is pinned by
# a test rather than left to this comment.
#
# Deliberately not the ``clamp(period / 2, ...)`` form: with no sub-daily
# frequency in the enum, every recurring period halves to well past the
# two-hour ceiling, so the formula would compute a number and then always
# discard it. Reintroduce it if an hourly or minutely frequency lands.
_RECURRING_GRACE = timedelta(hours=2)
_ONCE_GRACE = timedelta(minutes=15)


def catch_up_grace(frequency: ScheduleFrequency) -> timedelta:
    """How late an occurrence may be and still be worth firing.

    Past this, the dispatcher records ``too_late`` and fast-forwards past
    whatever else went stale during the gap rather than replaying it: a
    schedule that was due six times during an outage delivers at most once
    when it comes back — its most recent occurrence, if that one is still
    inside this window — not six reports at once.
    """
    if frequency is ScheduleFrequency.ONCE:
        return _ONCE_GRACE
    return _RECURRING_GRACE


def next_run_after(schedule: Schedule, after: datetime) -> datetime | None:
    """The first instant this schedule fires strictly after ``after``.

    ``None`` means there is no next run — only reachable for ``ONCE``, whose
    single instant is already spent. What the caller makes of that depends
    on why it asked: a one-shot that ran, or that was written off as too
    late, is retired ``COMPLETED``; one that was merely turned away is
    paused instead, since it has not had its turn.

    ``after`` must be timezone-aware, and only ever selects *which*
    occurrence comes back: the result always lands exactly on ``at_time`` in
    the schedule's zone, because the grid is recomputed from that column
    rather than stepped forward from the last run. So a turn that fired
    three seconds late does not drag the next one three seconds late.

    That is also what lets the dispatcher skip an outage in one step. It
    advances from ``max(occurrence, now - grace)`` rather than walking the
    schedule through every occurrence the outage missed: the stale ones are
    jumped in a single call, while one still inside its catch-up window is
    not — advancing from ``now()`` alone would jump that one too, losing a
    report that was only minutes late.
    """
    if after.tzinfo is None:
        raise ValueError("next_run_after requires an aware datetime")

    tz = ZoneInfo(schedule.timezone)
    local = after.astimezone(tz)

    if schedule.frequency is ScheduleFrequency.ONCE:
        # ``run_date`` is NOT NULL for ONCE (ck_schedule_frequency_fields).
        assert schedule.run_date is not None
        fired = _at(schedule.run_date, schedule, tz)
        return fired if fired > local else None

    # Two probes, and two is enough: the first firing date on or after today
    # either still lies ahead of ``after``, or it has already passed today —
    # in which case the next one after it necessarily lies ahead.
    #
    # "Two" counts probes here, not months — and the second probe is what
    # carries the guarantee. Since ``day_of_month`` may name a day some
    # months lack, "the one after it" is not necessarily next month's same
    # day: for the 31st it can be two months out. The second probe finds it
    # anyway because it runs the same stepping search, so a short month in
    # between costs no extra probe. Anyone optimising that call into a plain
    # "same day next month" would break the 31st silently.
    day = _first_firing_date(schedule, local.date())
    fired = _at(day, schedule, tz)
    if fired > local:
        return fired
    return _at(_first_firing_date(schedule, day + timedelta(days=1)), schedule, tz)


def _at(day: date, schedule: Schedule, tz: ZoneInfo) -> datetime:
    """The schedule's wall-clock time on ``day``, as a UTC instant.

    A local time that a DST transition skips resolves forward per PEP 495
    rather than raising, so a spring-forward morning still produces a run.
    """
    return datetime.combine(day, schedule.at_time, tzinfo=tz).astimezone(UTC)


def _first_firing_date(schedule: Schedule, on_or_after: date) -> date:
    """The first local date on or after ``on_or_after`` this schedule fires."""
    if schedule.frequency is ScheduleFrequency.DAILY:
        return on_or_after

    if schedule.frequency is ScheduleFrequency.WEEKLY:
        assert schedule.weekday is not None
        return on_or_after + timedelta(days=(schedule.weekday - on_or_after.weekday()) % 7)

    assert schedule.day_of_month is not None
    day = schedule.day_of_month
    year, month = on_or_after.year, on_or_after.month
    if on_or_after.day > day:
        year, month = _next_month(year, month)
    # A month that is too short simply does not fire — the cron reading of
    # "the 31st", and the one a user who picked it is asking for. February
    # is skipped by the 29th through the 31st, and the short months by the
    # 31st, so the search has to step rather than compute.
    #
    # A loop rather than a single "try, else next month" step, even though on
    # the Gregorian calendar one step always suffices. Why one is enough has
    # to hold for the widest day too: the months that lack a 31st are
    # February, April, June, September and November — pairwise non-adjacent,
    # and December has 31 so the year wrap is safe. (The narrower "only
    # February is short" reading covers days 29-30 but not 31, which is the
    # day that motivated widening the range.) Measured for every day 1-31
    # across 2024-2100 and pinned by
    # ``test_the_loop_never_needs_more_than_one_step``. The loop is here so
    # the function does not silently depend on that fact — it reads as
    # "search forward for a month that has this day", which is what it means.
    #
    # Twelve bounds it with an order of magnitude of slack. Exhausting a full
    # year would mean the calendar is wrong rather than the input, which is
    # why the exit is an assertion and not a fallback.
    for _ in range(12):
        if day <= monthrange(year, month)[1]:
            return date(year, month, day)
        year, month = _next_month(year, month)
    raise AssertionError(f"no month within a year has a day {day}")


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)
