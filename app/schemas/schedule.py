"""Schemas for the /api/schedules surface (scheduled tasks).

The recurrence spec is a **nested object on the way in and flat on the way
out**, and the asymmetry is deliberate. Inbound it has to be all-or-nothing:
which of ``run_date`` / ``weekday`` / ``day_of_month`` is required depends on
``frequency``, so a patch that changed the frequency without its companion
field would either be rejected by a database CHECK as a 500-shaped surprise
or, worse, stored. Nesting makes "replace the whole recurrence" the only
expressible edit, validated in one place. Outbound there is nothing to
enforce, and a flat row is what a renderer wants.
"""

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.common.text import printable
from app.common.timezones import is_known_timezone
from app.models.schedule import ScheduleFrequency, ScheduleSkipReason, ScheduleStatus
from app.models.session import Tier
from app.models.session_turn import SessionTurnStatus, TerminationReason
from app.schemas.session import CONNECTORS_MAX, SKILLS_MAX

# Which optional column each frequency requires, and which it forbids.
# Mirrors ``ck_schedule_frequency_fields``; this copy exists so the caller
# gets a 422 naming the field instead of a database error naming a
# constraint.
_REQUIRED_FIELD: dict[ScheduleFrequency, str | None] = {
    ScheduleFrequency.ONCE: "run_date",
    ScheduleFrequency.DAILY: None,
    ScheduleFrequency.WEEKLY: "weekday",
    ScheduleFrequency.MONTHLY: "day_of_month",
}
_SPEC_FIELDS = ("run_date", "weekday", "day_of_month")


class RecurrenceSpec(BaseModel):
    """When a schedule fires, in its owner's own wall clock."""

    frequency: ScheduleFrequency
    at_time: time
    # IANA name. Snapshotted onto the row rather than read from the user's
    # profile at fire time, so a standing 09:00 report stays at 09:00 for
    # whoever set it up.
    timezone: str = Field(..., min_length=1, max_length=64)
    run_date: date | None = None
    weekday: int | None = Field(default=None, ge=0, le=6, description="0 = Monday")
    day_of_month: int | None = Field(default=None, ge=1, le=31)

    @field_validator("at_time")
    @classmethod
    def _wall_clock_only(cls, value: time) -> time:
        """Reject an offset on the time itself.

        ``"09:00+00:00"`` parses, but the recurrence builds the instant with
        ``datetime.combine(day, at_time, tzinfo=<schedule zone>)``, and an
        explicit ``tzinfo`` overrides whatever the time carried — so the
        offset would be discarded in silence and the schedule would fire
        hours from where the caller asked. The zone belongs to the
        ``timezone`` field, which is the only place that decides it.
        """
        if value.tzinfo is not None:
            raise ValueError("at_time must be a wall-clock time, without an offset")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Accept an IANA zone name, and only that.

        ``ZoneInfo(value)`` is too permissive to validate with: it resolves
        anything present in the tz tree, ``"localtime"`` included.

        This check is still only as good as *this* host's tz database, and
        the background process may not have the same one. That residual gap
        is closed on the other side rather than here — the dispatcher pauses
        a schedule whose zone it cannot resolve, instead of retrying it
        forever (see ``schedule_dispatcher._zone_is_resolvable``).
        """
        if not is_known_timezone(value):
            raise ValueError(f"unknown timezone: {value!r}")
        return value

    @model_validator(mode="after")
    def _exactly_the_fields_this_frequency_uses(self) -> "RecurrenceSpec":
        required = _REQUIRED_FIELD[self.frequency]
        for name in _SPEC_FIELDS:
            supplied = getattr(self, name) is not None
            if name == required and not supplied:
                raise ValueError(f"{self.frequency.value} requires {name}")
            if name != required and supplied:
                raise ValueError(f"{self.frequency.value} does not use {name}")
        return self


def _checked_prompt(value: str | None) -> str | None:
    """Refuse a prompt that cannot be stored, or that says nothing.

    ``None`` passes: on ``PATCH`` it means "leave the prompt alone", and this
    validator is shared with the create body, where the field is required and
    ``None`` never arrives.

    U+0000 is not representable in a ``text`` column, so a prompt carrying one
    fails at INSERT as an opaque 500 — and prompts do carry them, since some
    models emit null bytes in garbled output that a user may paste straight
    back in. Refused rather than stripped: silently rewriting what someone
    typed is worse than telling them.

    A prompt with nothing printable in it — zero-width spaces, say — passes
    ``min_length`` and would be delivered as a turn with no instruction. It is
    judged by :func:`~app.common.text.printable`, the same function the
    dispatcher titles the session with, so the two cannot disagree about
    whether there is anything there.
    """
    if value is None:
        return None
    if "\x00" in value:
        raise ValueError("prompt must not contain a null character")
    if not printable(value).strip():
        raise ValueError("prompt must contain visible text")
    return value


class ScheduleCreate(BaseModel):
    """Body of ``POST /api/schedules``."""

    # The message the agent receives, verbatim. A scheduled turn has nobody
    # to answer a clarifying question, so a prompt that needs one spends an
    # occurrence asking it.
    prompt: str = Field(..., min_length=1, max_length=8000)
    recurrence: RecurrenceSpec
    tier: Tier = Tier.STANDARD
    # Whether each firing's session thinks ("deep think"), on by default as
    # in a new session.
    thinking_enabled: bool = True
    # Skill *names*, the same shape the composer sends. Since the worker's
    # listing narrowed to live SYSTEM skills plus the turn's own picks,
    # this is the only way an OFFICIAL or CUSTOM skill reaches a scheduled
    # run. Names that no longer resolve are dropped at each firing rather
    # than rejected here — a schedule outlives the skills it names.
    skills: list[str] = Field(default_factory=list, max_length=SKILLS_MAX)
    # ``mcp_server`` ids. Ownership is checked in the endpoint; liveness is
    # not checked at all here, because a Connector disconnected today may
    # well be reconnected before this task next fires.
    connectors: list[int] = Field(default_factory=list, max_length=CONNECTORS_MAX)
    # Run each firing in this project. Ownership is checked in the endpoint.
    project_id: UUID | None = None

    _check_prompt = field_validator("prompt")(_checked_prompt)


class ScheduleUpdate(BaseModel):
    """Body of ``PATCH /api/schedules/{id}``.

    ``status`` accepts only the two states an owner can choose. COMPLETED is
    reached by a one-shot firing, never by asking.

    ``project_id`` is read through ``model_fields_set``, as on
    ``PATCH /sessions/{id}``: an id moves the schedule into that project,
    ``null`` moves it out, and leaving it out changes nothing.
    """

    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    recurrence: RecurrenceSpec | None = None
    tier: Tier | None = None
    thinking_enabled: bool | None = None
    # Whole-set replacements, not deltas: the form submits what it wants,
    # and ``None`` means "leave alone" while ``[]`` means "clear". A patch
    # that sent one id at a time would need the two to be distinguishable
    # anyway, and the form has no per-item control to justify it.
    skills: list[str] | None = Field(default=None, max_length=SKILLS_MAX)
    connectors: list[int] | None = Field(default=None, max_length=CONNECTORS_MAX)
    status: Literal[ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED] | None = None
    project_id: UUID | None = None

    _check_prompt = field_validator("prompt")(_checked_prompt)


class ScheduleRunResponse(BaseModel):
    """One occurrence the dispatcher acted on, and how it went.

    Exactly one of three shapes. Skipped: ``skip_reason`` is set and nothing
    else is. Ran: ``turn_status`` is set, from the *first* turn of the
    session it created — the turn the dispatcher queued, not whatever the
    owner went on to send in that session afterwards. Neither: a row no
    current path writes, or a session hard-deleted along with its turns; the
    client renders it as "did not run".

    Nothing here is stored on the occurrence. The outcome is joined from the
    turn, so the history cannot disagree with it.
    """

    id: int
    scheduled_for: datetime
    # The session to open, or ``None`` once there is none to open — never
    # created, or deleted since. Deliberately not the raw foreign key: a
    # history row pointing at a session the owner deleted is a link the
    # session endpoint refuses. The outcome survives the deletion; the link
    # does not.
    session_id: str | None
    turn_status: SessionTurnStatus | None
    skip_reason: ScheduleSkipReason | None
    # Why the turn ended, when that says more than ``turn_status`` does — a
    # timeout and a model error are both FAILED. ``None`` for a turn that
    # completed normally or has not ended.
    termination_reason: TerminationReason | None = None
    # Seconds the turn took, for runs that finished. What "it works" looks
    # like is partly how long it takes; a report that started taking six
    # minutes instead of forty seconds is a change worth seeing without
    # opening anything.
    #
    # Measured from *enqueue*, so it includes any wait behind a busy fleet —
    # there is no claim-time column to subtract. A jump may therefore be
    # queueing rather than the agent slowing down. ``None`` for a turn still
    # running, and for one whose two timestamps disagree about direction.
    duration_seconds: float | None = None
    read_at: datetime | None
    created_at: datetime


class ScheduleResponse(BaseModel):
    """One schedule, as the management list renders it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    prompt: str
    status: ScheduleStatus
    frequency: ScheduleFrequency
    at_time: time
    timezone: str
    run_date: date | None
    weekday: int | None
    day_of_month: int | None
    # NULL once a one-shot has fired — there is no next run to show.
    next_run_at: datetime | None
    # The most recent occurrence, whole — its time and its outcome together,
    # rendered exactly as the history renders it. ``None`` before the first.
    # Assembled by the endpoint, like ``connectors``.
    last_run: ScheduleRunResponse | None = None
    tier: Tier
    thinking_enabled: bool
    # What each firing will carry. ``skills`` reads straight off the row;
    # ``connectors`` is assembled from ``schedule_mcp_server``.
    skills: list[str]
    connectors: list[int] = Field(default_factory=list)
    # The live project each firing runs in, or ``None`` — also when the row
    # names a project since deleted (see ``Schedule.project_id``). Assembled
    # by the endpoint, like ``connectors``.
    project_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


# How many unread occurrences one listing acknowledges. Bounds the id list
# both ways — the response carrying it and the mark-read echoing it back. An
# owner past it (months of runs never opened) is caught up over a few visits,
# the remainder staying unread in between: the harmless direction.
UNREAD_ACKNOWLEDGE_MAX = 500


class ScheduleListResponse(BaseModel):
    """The caller's schedules, and the unread results this request observed.

    Opening the page counts as having seen every settled result — older runs
    included, not only the latest one each card displays. ``unread_run_ids``
    is that set as the list request observed it: the unread occurrences it
    saw skipped or with their first turn ended, oldest first and at most
    ``UNREAD_ACKNOWLEDGE_MAX`` of them. The client hands the ids back
    to ``POST /schedules/runs/read`` once the list has rendered, so an
    occurrence the request observed still running stays news when it
    finishes, and a page that failed to load marks nothing because it never
    received an id to send.

    Ids rather than a timestamp: a time cannot say what a query *observed*. A
    turn stamps ``terminated_at`` when its transaction starts and becomes
    visible only when that transaction commits, so a cutoff taken in between
    would cover a result the request still saw as running.
    """

    items: list[ScheduleResponse]
    unread_run_ids: list[int] = Field(max_length=UNREAD_ACKNOWLEDGE_MAX)


class MarkRunsReadRequest(BaseModel):
    """Body of ``POST /schedules/runs/read``."""

    # The ``unread_run_ids`` of the listing the caller displayed. See
    # :class:`ScheduleListResponse`.
    run_ids: list[int] = Field(max_length=UNREAD_ACKNOWLEDGE_MAX)


class UnreadCountResponse(BaseModel):
    count: int
