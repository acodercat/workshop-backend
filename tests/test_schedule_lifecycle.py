"""What an owner's edit does to a schedule's status and clock.

``lifecycle`` is pure over a row and a supplied ``now``, so every transition
is a row in a table here rather than a race to stage against a database.
The row lock that makes the decisions safe is pinned beside the endpoint
(``tests/test_schedule_endpoint_queries.py``).
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.errors import (
    ScheduleCompletedError,
    ScheduleRunTimePastError,
    ScheduleTimezoneUnavailableError,
)
from app.models.schedule import Schedule, ScheduleFrequency, ScheduleStatus
from app.schemas.schedule import RecurrenceSpec
from app.services.schedule import lifecycle

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def _daily(**overrides) -> Schedule:
    fields = dict(
        status=ScheduleStatus.ACTIVE,
        frequency=ScheduleFrequency.DAILY,
        at_time=time(9, 0),
        timezone="UTC",
        run_date=None,
        weekday=None,
        day_of_month=None,
        # Due an hour ago and still inside its two-hour catch-up window.
        next_run_at=datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
        consecutive_failures=0,
    )
    fields.update(overrides)
    return Schedule(**fields)


def _spec_of(schedule: Schedule, **overrides) -> RecurrenceSpec:
    fields = {
        name: getattr(schedule, name)
        for name in ("frequency", "at_time", "timezone", "run_date", "weekday", "day_of_month")
    }
    fields.update(overrides)
    return RecurrenceSpec(**fields)


class TestTheClockMovesOnlyWhenTheTimeDoes:
    def test_resubmitting_the_same_recurrence_keeps_a_pending_occurrence(self):
        # The form sends the recurrence it was opened with on every save. Read
        # as "the time moved", a wording fix recomputed from now and dropped
        # the occurrence that was due and still deliverable.
        schedule = _daily()
        pending = schedule.next_run_at

        activating = lifecycle.apply_owner_edit(
            schedule, recurrence=_spec_of(schedule), status=None, now=NOW
        )

        assert activating is False
        assert schedule.next_run_at == pending

    def test_a_changed_time_recomputes_from_now(self):
        schedule = _daily()

        lifecycle.apply_owner_edit(
            schedule, recurrence=_spec_of(schedule, at_time=time(18, 0)), status=None, now=NOW
        )

        assert schedule.at_time == time(18, 0)
        assert schedule.next_run_at == datetime(2026, 9, 14, 18, 0, tzinfo=UTC)

    def test_an_edit_to_a_past_one_shot_that_keeps_its_time_is_allowed(self):
        # Still due and not yet dispatched: changing its wording must neither
        # refuse ("that time has passed") nor move it.
        due = NOW - timedelta(minutes=5)
        schedule = _daily(
            frequency=ScheduleFrequency.ONCE,
            run_date=due.date(),
            at_time=due.time().replace(tzinfo=None),
            next_run_at=due,
        )

        lifecycle.apply_owner_edit(schedule, recurrence=_spec_of(schedule), status=None, now=NOW)

        assert schedule.next_run_at == due
        assert schedule.status is ScheduleStatus.ACTIVE

    def test_moving_a_one_shot_into_the_past_is_refused(self):
        schedule = _daily(
            frequency=ScheduleFrequency.ONCE,
            run_date=date(2026, 9, 20),
            next_run_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(ScheduleRunTimePastError):
            lifecycle.apply_owner_edit(
                schedule,
                recurrence=_spec_of(schedule, run_date=date(2026, 9, 1)),
                status=None,
                now=NOW,
            )


class TestStatus:
    def test_retiming_a_completed_one_shot_reactivates_it(self):
        # What the dispatcher may have done a moment before the edit took the
        # lock. A next run stored on a COMPLETED row is one its ACTIVE scan
        # never fires, so a new time has to bring the status with it.
        schedule = _daily(
            frequency=ScheduleFrequency.ONCE,
            run_date=date(2026, 9, 13),
            status=ScheduleStatus.COMPLETED,
            next_run_at=None,
            consecutive_failures=2,
        )

        activating = lifecycle.apply_owner_edit(
            schedule,
            recurrence=_spec_of(schedule, run_date=date(2026, 9, 15)),
            status=None,
            now=NOW,
        )

        assert activating is True
        assert schedule.status is ScheduleStatus.ACTIVE
        assert schedule.next_run_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
        assert schedule.consecutive_failures == 0

    def test_a_completed_one_shot_edited_without_a_new_time_stays_completed(self):
        schedule = _daily(
            frequency=ScheduleFrequency.ONCE,
            run_date=date(2026, 9, 13),
            status=ScheduleStatus.COMPLETED,
            next_run_at=None,
        )

        activating = lifecycle.apply_owner_edit(
            schedule, recurrence=_spec_of(schedule), status=None, now=NOW
        )

        assert activating is False
        assert schedule.status is ScheduleStatus.COMPLETED
        assert schedule.next_run_at is None

    def test_pausing_a_completed_one_is_refused(self):
        schedule = _daily(status=ScheduleStatus.COMPLETED, next_run_at=None)

        with pytest.raises(ScheduleCompletedError):
            lifecycle.apply_owner_edit(
                schedule, recurrence=None, status=ScheduleStatus.PAUSED, now=NOW
            )

    def test_retiming_a_paused_schedule_leaves_it_paused(self):
        # Pausing was deliberate; moving the time is not a request to undo it.
        schedule = _daily(status=ScheduleStatus.PAUSED)

        activating = lifecycle.apply_owner_edit(
            schedule, recurrence=_spec_of(schedule, at_time=time(18, 0)), status=None, now=NOW
        )

        assert activating is False
        assert schedule.status is ScheduleStatus.PAUSED
        assert schedule.next_run_at == datetime(2026, 9, 14, 18, 0, tzinfo=UTC)

    def test_resuming_rearms_from_now_and_clears_the_streak(self):
        schedule = _daily(
            status=ScheduleStatus.PAUSED,
            next_run_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
            consecutive_failures=3,
        )

        activating = lifecycle.apply_owner_edit(
            schedule, recurrence=None, status=ScheduleStatus.ACTIVE, now=NOW
        )

        assert activating is True
        assert schedule.next_run_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
        assert schedule.consecutive_failures == 0

    def test_pausing_keeps_the_clock(self):
        schedule = _daily()
        pending = schedule.next_run_at

        lifecycle.apply_owner_edit(schedule, recurrence=None, status=ScheduleStatus.PAUSED, now=NOW)

        assert schedule.status is ScheduleStatus.PAUSED
        assert schedule.next_run_at == pending

    def test_resuming_an_active_schedule_is_not_an_activation(self):
        # Nothing to count against the cap, and no reason to move the clock.
        schedule = _daily()
        pending = schedule.next_run_at

        activating = lifecycle.apply_owner_edit(
            schedule, recurrence=None, status=ScheduleStatus.ACTIVE, now=NOW
        )

        assert activating is False
        assert schedule.next_run_at == pending


class TestArm:
    def test_a_zone_this_host_no_longer_knows_is_refused_not_raised(self):
        schedule = _daily(timezone="Mars/Olympus_Mons")

        with pytest.raises(ScheduleTimezoneUnavailableError):
            lifecycle.arm(schedule, NOW)

    def test_assign_reports_whether_anything_changed(self):
        schedule = _daily()
        assert lifecycle.assign_recurrence(schedule, _spec_of(schedule)) is False
        assert lifecycle.assign_recurrence(schedule, _spec_of(schedule, weekday=None)) is False
        assert lifecycle.assign_recurrence(schedule, _spec_of(schedule, at_time=time(8, 0))) is True
