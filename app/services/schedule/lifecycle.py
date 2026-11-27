"""What an owner's action does to a schedule's status and clock.

Pure functions over a ``Schedule`` row, like :mod:`.recurrence`: no I/O and
no reading of the clock, so every transition can be tested as a table. The
caller supplies ``now`` and owns everything around the call — loading the
row **under** ``FOR UPDATE`` first, checking the active cap when an edit
activates, and committing.

The lock is not incidental. The dispatcher settles an occurrence under the
same row lock, and it moves exactly the fields decided here: ``status``
(a one-shot completing, a schedule auto-pausing) and ``next_run_at``. An
edit that decided on an unlocked read could therefore act on a state that
no longer exists — reactivate nothing, or store a next run on a row the
dispatcher had just completed, which its ``ACTIVE`` scan then never fires.
"""

from datetime import datetime

from app.common.timezones import is_known_timezone
from app.errors import (
    ScheduleCompletedError,
    ScheduleRunTimePastError,
    ScheduleTimezoneUnavailableError,
)
from app.models.schedule import Schedule, ScheduleStatus
from app.schemas.schedule import RecurrenceSpec
from app.services.schedule.recurrence import next_run_after

# The columns a ``RecurrenceSpec`` is stored in, named alike on both sides.
_RECURRENCE_FIELDS = (
    "frequency",
    "at_time",
    "timezone",
    "run_date",
    "weekday",
    "day_of_month",
)


def assign_recurrence(schedule: Schedule, spec: RecurrenceSpec) -> bool:
    """Store *spec* on the row. Returns whether that changed anything.

    The return value is the point. A form resubmits the recurrence it was
    opened with whether or not the owner touched it, and "the body carries a
    recurrence" is not "the time moved": treating it so recomputed
    ``next_run_at`` from now on every wording fix, silently dropping an
    occurrence that was due and still inside its catch-up window.
    """
    changed = False
    for field in _RECURRENCE_FIELDS:
        value = getattr(spec, field)
        if getattr(schedule, field) != value:
            setattr(schedule, field, value)
            changed = True
    return changed


def arm(schedule: Schedule, now: datetime) -> None:
    """Point the schedule at its first occurrence from *now*.

    Refuses a one-shot whose instant has passed rather than storing it with
    no next run: it would be retired on the first tick that saw it — a
    schedule the owner saved and that never existed.

    The zone was validated when it was written, but a row can outlive the tz
    database it was written against (an image rollback, or an API and a
    worker on different ones), and the recurrence would raise from inside
    rather than refuse. The dispatcher closes the same gap on its side.
    """
    if not is_known_timezone(schedule.timezone):
        raise ScheduleTimezoneUnavailableError(schedule.timezone)
    next_run = next_run_after(schedule, now)
    if next_run is None:
        raise ScheduleRunTimePastError("That run time has already passed.")
    schedule.next_run_at = next_run


def apply_owner_edit(
    schedule: Schedule,
    *,
    recurrence: RecurrenceSpec | None,
    status: ScheduleStatus | None,
    now: datetime,
) -> bool:
    """Apply an edit's recurrence and status to a locked row.

    ``None`` leaves either alone. Returns whether the edit *activates* the
    schedule, which the caller must check against the active cap before it
    commits.

    - The clock moves only when the recurrence actually changed, or the
      schedule is being activated. Anything else — a prompt, a skill, an
      attachment — leaves a pending occurrence exactly where it was.
    - Retiming a ``COMPLETED`` one-shot reactivates it: it has no other way
      back, and a next run stored on a completed row is one the dispatcher
      never honours. A ``PAUSED`` schedule stays paused when retimed —
      pausing was deliberate, and moving the time is not a request to undo
      it.
    - Pausing a ``COMPLETED`` one is refused. It would strand the row as
      PAUSED with no next run, which no resume can leave, and drop the "this
      already ran" its owner was reading.
    - Activation resets the failure streak, or a schedule paused by repeated
      failure would be one failure from pausing again the moment it resumed.
    """
    if schedule.status is ScheduleStatus.COMPLETED and status is ScheduleStatus.PAUSED:
        raise ScheduleCompletedError()
    retimed = recurrence is not None and assign_recurrence(schedule, recurrence)
    if status is None and retimed and schedule.status is ScheduleStatus.COMPLETED:
        status = ScheduleStatus.ACTIVE
    activating = status is ScheduleStatus.ACTIVE and schedule.status is not ScheduleStatus.ACTIVE
    if status is not None:
        schedule.status = status
    if retimed or activating:
        arm(schedule, now)
    if activating:
        schedule.consecutive_failures = 0
    return activating
