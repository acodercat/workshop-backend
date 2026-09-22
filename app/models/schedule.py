"""Scheduled tasks — a user's standing instruction to run an agent turn on a clock.

Two tables. ``schedule`` is the definition the user manages (what to run,
how often, in which timezone); ``schedule_run`` is one row per *occurrence*
the dispatcher acted on.

The dispatcher (``app/workers/schedule_dispatcher.py``) does not run
anything itself — it calls ``queue_user_message`` exactly like the message
endpoint does, so a scheduled turn is indistinguishable from a typed one by
the time the worker claims it. That is why neither the worker nor axon knows
this feature exists.

**``schedule_run`` deliberately stores no execution status.** An
occurrence's outcome is the *first turn* of the session it enqueued — the
turn the dispatcher queued — reached through ``session_id`` and read by a
join. Not ``session.status``: a scheduled session is an ordinary session
once it exists, so its owner can keep chatting in it, and its status then
describes whatever turn ran last rather than whether the scheduled one
worked. Copying the status here instead would need a synchronisation
mechanism to keep two truths agreeing, and the losing side of that race is
always the copy. Sessions are soft-deleted and ``session_turn`` is never
pruned, so the join does not go hollow over time.

What the row *does* carry is the two things the session cannot know:
``scheduled_for`` (which occurrence this was — the idempotency key) and
``skip_reason`` (why no session was created at all).
"""

import uuid
from datetime import date, datetime, time
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base
from app.models.session import AgentType, Tier


class ScheduleStatus(StrEnum):
    """Whether the dispatcher will act on this schedule.

    - ``ACTIVE`` — due occurrences fire.
    - ``PAUSED`` — the owner suspended it; or it auto-suspended after
      ``schedule_dispatcher._FAILURES_BEFORE_PAUSE`` consecutive failures;
      or it is a one-shot that was turned away (no credits, at the
      concurrency cap) and so has not had its turn. Configuration is
      retained, and resuming recomputes ``next_run_at``.
    - ``COMPLETED`` — a ``ONCE`` schedule with nothing left to do: it ran,
      or its instant passed so far back that it was written off. Terminal,
      and ``next_run_at`` is NULL to say so.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class ScheduleFrequency(StrEnum):
    """How often a schedule recurs.

    A closed set of shapes rather than a cron expression: users never type
    cron, and every shape here is renderable as a sentence in both UI
    languages. ``app/services/schedule/recurrence.py`` is the only place that
    turns one of these into an instant.
    """

    ONCE = "ONCE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class ScheduleSkipReason(StrEnum):
    """Why an occurrence produced no session — and what that means for the
    schedule.

    Every member means "this occurrence was not spent", never "it ran and
    failed": a run that reached the agent has a ``session_id`` and reports
    through that session's first turn instead.

    A reason carries its own consequences (:attr:`counts_as_failure`,
    :attr:`retires_one_shot`) so the dispatcher records every skip the
    same way and no caller has to remember which flag a reason implies.

    Stored as a VARCHAR (``native_enum=False``) because the set grows as new
    refusal paths appear, and a new member should not cost an ``ALTER TYPE``.
    Values are lowercase, like ``session_turn.termination_reason``.
    """

    INSUFFICIENT_CREDITS = "insufficient_credits"
    # The owner is at their running-task cap for the whole catch-up window.
    CONCURRENCY_LIMIT = "concurrency_limit"
    # The dispatcher reached the occurrence after its catch-up window closed.
    TOO_LATE = "too_late"
    # The stored tier no longer resolves to a runnable model — a deployment
    # fault, but one the owner has to notice.
    MISCONFIGURED = "misconfigured"
    # An attachment cannot be bound: its object is gone, it is over the cap,
    # or its key is held by someone else.
    ATTACHMENT_UNAVAILABLE = "attachment_unavailable"

    @property
    def counts_as_failure(self) -> bool:
        """Whether only the owner can clear it, and so whether it counts
        toward the automatic pause. Congestion and lateness are the
        platform's own call and resolve without them.

        Mirrored by the frontend's ``SCHEDULE_SKIP_REASONS_COUNTED_AS_FAILURE``,
        which decides what the run history and the card paint red."""
        return self in {
            ScheduleSkipReason.INSUFFICIENT_CREDITS,
            ScheduleSkipReason.MISCONFIGURED,
            ScheduleSkipReason.ATTACHMENT_UNAVAILABLE,
        }

    @property
    def retires_one_shot(self) -> bool:
        """Whether a one-shot skipped for this reason is finished rather than
        paused. Only lateness is final — its moment has passed. Every other
        reason can clear, so the one-shot keeps its instant and waits for the
        owner to re-arm it."""
        return self is ScheduleSkipReason.TOO_LATE


class Schedule(Base):
    __tablename__ = "schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # The message text delivered to the agent, verbatim, and the only thing
    # identifying this schedule — there is no separate name. Asking for one
    # was asking the user to summarise what they had just written; the list
    # shows the prompt, and the session each run creates takes its title
    # from it too (``domain`` note in ``schedule_dispatcher._enqueue``).
    #
    # A scheduled turn has nobody to answer a clarifying question, so a
    # prompt that needs one burns an occurrence producing "which report did
    # you mean?" — the create form says so rather than the schema enforcing
    # it.
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # Skill *names*, not ids, and not a snapshot — the same shape the
    # composer sends (``MessageCreate.skills``), re-resolved at every
    # firing by ``_filter_invokable_skills``. Storing names is what makes
    # a skill the owner re-uploads take effect on the next run instead of
    # pinning the run to bytes chosen months ago.
    #
    # Names rather than a foreign-keyed table for two reasons: a CUSTOM
    # skill shadows a same-named platform one, so the name is the real
    # selector; and the resolver already drops what no longer resolves,
    # which is the behaviour a stale pick should have. The cost is that a
    # retired or deleted skill goes silently missing — the form flags
    # names it cannot resolve, since nothing else would tell the owner.
    #
    # A collection (0..N): NOT NULL DEFAULT '[]' so "no skills" is the
    # empty array, never NULL — readers iterate without a null-guard.
    # Same convention as ``session_turn.skills``.
    skills: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    status: Mapped[ScheduleStatus] = mapped_column(
        SAEnum(
            ScheduleStatus,
            name="schedule_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )

    # --- Recurrence spec. Which of the three optional columns is required
    # --- is decided by ``frequency`` and enforced by a CHECK below.
    frequency: Mapped[ScheduleFrequency] = mapped_column(
        SAEnum(
            ScheduleFrequency,
            name="schedule_frequency",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # Wall-clock time in ``timezone``, not UTC — "09:00 in Shanghai" has to
    # survive a DST transition as 09:00, which a stored UTC offset cannot.
    at_time: Mapped[time] = mapped_column(Time, nullable=False)
    run_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 0 = Monday, matching ``datetime.weekday()`` so the recurrence math
    # needs no translation table.
    weekday: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 1-31. A month too short to have the chosen day simply does not fire —
    # the cron reading, and the one a user picking the 31st is asking for
    # ("month end" as a distinct option would be its own semantics, and is
    # not this column). ``_first_firing_date`` steps past short months
    # rather than computing, which is why the range can be this wide.
    day_of_month: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # IANA name, snapshotted at creation and NOT read back from the user.
    # A user who travels and changes their locale must not silently move
    # every standing 9am report to 8am; moving them is an explicit action.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Clock. ``next_run_at`` is the dispatcher's only scan column.
    # Nullable because a COMPLETED schedule has no next run, and saying so
    # with NULL beats parking a stale instant nobody will honour. There is no
    # "last run" column: the latest ``schedule_run`` is that, with its
    # outcome attached, where a timestamp kept here could only ever describe
    # a different occurrence from the outcome shown beside it.
    next_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Reset to 0 by any occurrence that enqueues. Crossing
    # ``schedule_dispatcher._FAILURES_BEFORE_PAUSE`` pauses the schedule —
    # which stops both the repeated notification and the repeated spend,
    # the two ways a permanently-broken schedule hurts its owner.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # --- Session construction. Every column here is required to build a
    # --- Session; only ``tier`` is exposed by the v1 create form.
    tier: Mapped[Tier] = mapped_column(
        SAEnum(
            Tier,
            name="session_tier",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    agent_type: Mapped[AgentType] = mapped_column(
        SAEnum(
            AgentType,
            name="agent_type",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    thinking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # The dispatcher's scan: due-and-active, oldest first. Partial on
        # ACTIVE so paused and completed rows cost nothing to skip, which
        # is most of the table once a deployment has been running a while.
        Index(
            "ix_schedule_due",
            "next_run_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        # The management list: mine, newest first. Also the index Postgres
        # does not create for the FK, so deleting a user does not seq-scan.
        Index("ix_schedule_user_id_created_at", "user_id", text("created_at DESC")),
        # Each frequency owns exactly one shape. Without this a WEEKLY row
        # with no weekday is storable, and the recurrence math would have
        # to invent an answer for it at dispatch time — in a background
        # process, where nobody is watching.
        CheckConstraint(
            "(frequency = 'ONCE' AND run_date IS NOT NULL"
            " AND weekday IS NULL AND day_of_month IS NULL)"
            " OR (frequency = 'DAILY' AND run_date IS NULL"
            " AND weekday IS NULL AND day_of_month IS NULL)"
            " OR (frequency = 'WEEKLY' AND run_date IS NULL"
            " AND weekday IS NOT NULL AND day_of_month IS NULL)"
            " OR (frequency = 'MONTHLY' AND run_date IS NULL"
            " AND weekday IS NULL AND day_of_month IS NOT NULL)",
            name="ck_schedule_frequency_fields",
        ),
        CheckConstraint(
            "weekday IS NULL OR (weekday BETWEEN 0 AND 6)",
            name="ck_schedule_weekday_range",
        ),
        CheckConstraint(
            "day_of_month IS NULL OR (day_of_month BETWEEN 1 AND 31)",
            name="ck_schedule_day_of_month_range",
        ),
    )

    def __repr__(self) -> str:
        return f"<Schedule(id={self.id}, status='{self.status.value}', next={self.next_run_at})>"


class ScheduleRun(Base):
    __tablename__ = "schedule_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schedule.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized from ``schedule.user_id`` (an immutable relationship, so
    # the copy cannot go stale) to make the unread-count query a single
    # indexed scan with no join — same posture as ``session_site.user_id``.
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # Which occurrence this row is: the ``next_run_at`` the dispatcher
    # claimed, not the instant it acted. Paired with ``schedule_id`` in a
    # unique index, this is what makes dispatch at-most-once across
    # restarts, replicas, and clock drift.
    scheduled_for: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Non-NULL ⟺ the occurrence enqueued a turn. ``SET NULL`` rather than
    # CASCADE so a hard-deleted session leaves the history row standing —
    # "this ran, the session is gone" is a truer answer than a missing row.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.id", ondelete="SET NULL"), nullable=True
    )
    # Non-NULL ⟺ the occurrence was refused before spending anything.
    # Exactly one of the two is set on every row the dispatcher writes: both
    # are staged inside the transaction the enqueue commits, so an
    # interrupted dispatcher rolls the row back rather than committing it
    # half-formed. ``ck_schedule_run_outcome`` still forbids *both* being
    # set — a guard against a future path, not a state produced today.
    skip_reason: Mapped[ScheduleSkipReason | None] = mapped_column(
        SAEnum(
            ScheduleSkipReason,
            native_enum=False,
            create_constraint=False,
            length=32,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The idempotency key. Two dispatchers racing the same occurrence,
        # or one retrying after a crash, collide here instead of running
        # the user's task twice.
        Index(
            "uq_schedule_run_occurrence",
            "schedule_id",
            "scheduled_for",
            unique=True,
        ),
        # The sidebar's unread badge.
        Index(
            "ix_schedule_run_unread",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("read_at IS NULL"),
        ),
        # One schedule's history, newest first. Its leading column also
        # covers the ``schedule_id`` FK.
        Index("ix_schedule_run_schedule_id_created_at", "schedule_id", text("created_at DESC")),
        # The ``user_id`` FK's own index. ``ix_schedule_run_unread`` above
        # leads on the same column but is *partial*, so Postgres cannot use
        # it for the reference check a ``DELETE FROM "user"`` runs — and this
        # is the table that grows. Same reasoning as ``schedule``'s
        # ``ix_schedule_user_id_created_at``, which is not partial and
        # therefore already serves double duty.
        Index("ix_schedule_run_user_id", "user_id"),
        # A row cannot both have enqueued a session and have been refused.
        CheckConstraint(
            "NOT (session_id IS NOT NULL AND skip_reason IS NOT NULL)",
            name="ck_schedule_run_outcome",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduleRun(id={self.id}, schedule_id={self.schedule_id}, for={self.scheduled_for})>"
        )
