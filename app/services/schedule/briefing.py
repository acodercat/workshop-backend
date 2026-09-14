"""What the agent is told about the scheduled task it is running.

A scheduled turn reaches the worker as an ordinary turn — same enqueue, same
claim — so without this the model would treat it as something the user just
typed. Two things it gets wrong as a result: it asks a clarifying question
nobody is there to answer, spending the occurrence on "which report did you
mean?"; and it reads "today" or "this week" against the host clock rather
than the owner's zone and the instant the run was meant for, which a late
catch-up run makes visibly different.

The text is rendered here, in the backend, and carried on the turn as a
plain ``<system-reminder>`` string (``session_turn.reminders``). The worker
forwards it verbatim, so it needs no knowledge of schedules. English, like
the rest of the agent's system-side text; the model answers in the user's
language regardless.

Pure functions over a ``Schedule`` row, like :mod:`.recurrence`: no I/O.
The caller must have checked that ``schedule.timezone`` resolves — the
dispatcher does, before it enqueues.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.schedule import Schedule, ScheduleFrequency

# Indexed by ``datetime.weekday()`` (Monday = 0), which ``Schedule.weekday``
# also follows. Spelled out rather than taken from ``calendar.day_name`` or
# ``%A``, both of which follow the process locale.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def render_run_reminder(schedule: Schedule, occurrence: datetime) -> str:
    """The reminder for the run of ``schedule`` due at ``occurrence``.

    ``occurrence`` is the instant the run was scheduled for (aware, any
    zone), not when it was enqueued: a catch-up run delivered at 10:30 is
    still the 09:00 report, and its dates should read that way.
    """
    local = occurrence.astimezone(ZoneInfo(schedule.timezone))
    return (
        "This turn was started by one of the user's scheduled tasks, not typed "
        f"by the user just now. The task runs {_describe_recurrence(schedule)}; "
        f"this run was scheduled for {_WEEKDAYS[local.weekday()]}, "
        f"{local.isoformat(sep=' ', timespec='minutes')} ({schedule.timezone}).\n"
        "Nobody is watching this run. Do not ask clarifying questions or wait "
        "for confirmation: where the task is ambiguous, make a reasonable "
        "assumption, say what you assumed, and complete it. Read relative "
        'dates such as "today" or "this week" against the scheduled time '
        "above."
    )


def _describe_recurrence(schedule: Schedule) -> str:
    """The recurrence as the tail of "The task runs …"."""
    at = f"{schedule.at_time:%H:%M}"
    match schedule.frequency:
        case ScheduleFrequency.ONCE:
            return "once"
        case ScheduleFrequency.DAILY:
            return f"every day at {at}"
        case ScheduleFrequency.WEEKLY:
            # NOT NULL for WEEKLY (``ck_schedule_frequency_fields``).
            assert schedule.weekday is not None
            return f"every {_WEEKDAYS[schedule.weekday]} at {at}"
        case ScheduleFrequency.MONTHLY:
            assert schedule.day_of_month is not None
            return f"on day {schedule.day_of_month} of every month at {at}"
