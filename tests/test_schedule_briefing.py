"""The reminder a scheduled run hands the model.

Pure over an unsaved ``Schedule``, like the recurrence tests: build a spec,
name the occurrence, read the text.
"""

from datetime import UTC, date, datetime, time

import pytest

from app.models.schedule import Schedule, ScheduleFrequency
from app.services.schedule import render_run_reminder

# 01:00 UTC is 09:00 in Shanghai the same day, and 21:00 the previous day
# in New York (EDT) — enough to show the zone decides the date, not UTC.
OCCURRENCE = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)


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


class TestRecurrence:
    @pytest.mark.parametrize(
        ("overrides", "sentence"),
        [
            ({}, "The task runs every day at 09:00;"),
            (
                {"frequency": ScheduleFrequency.WEEKLY, "weekday": 0},
                "The task runs every Monday at 09:00;",
            ),
            (
                {"frequency": ScheduleFrequency.MONTHLY, "day_of_month": 31},
                "The task runs on day 31 of every month at 09:00;",
            ),
            (
                {"frequency": ScheduleFrequency.ONCE, "run_date": date(2026, 9, 14)},
                "The task runs once;",
            ),
        ],
    )
    def test_each_frequency_reads_as_a_sentence(self, overrides, sentence):
        assert sentence in render_run_reminder(_schedule(**overrides), OCCURRENCE)

    def test_weekday_counts_from_monday(self):
        schedule = _schedule(frequency=ScheduleFrequency.WEEKLY, weekday=6)
        assert "every Sunday at" in render_run_reminder(schedule, OCCURRENCE)


class TestScheduledFor:
    def test_the_occurrence_reads_in_the_schedules_zone(self):
        text = render_run_reminder(_schedule(), OCCURRENCE)
        assert "scheduled for Monday, 2026-09-14 09:00+08:00 (Asia/Shanghai)." in text

    def test_the_zone_can_move_the_date(self):
        text = render_run_reminder(_schedule(timezone="America/New_York"), OCCURRENCE)
        assert "scheduled for Sunday, 2026-09-13 21:00-04:00 (America/New_York)." in text


def test_tells_the_model_nobody_is_there_to_answer():
    text = render_run_reminder(_schedule(), OCCURRENCE)
    assert "not typed by the user just now" in text
    assert "Do not ask clarifying questions" in text
