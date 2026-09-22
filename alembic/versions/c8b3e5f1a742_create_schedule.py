"""create schedule + schedule_run

Scheduled tasks: a standing instruction to run an agent turn on a clock.
``schedule`` is what the user manages; ``schedule_run`` is one row per
occurrence the dispatcher acted on.

Purely additive — two new tables, two new enum types, no existing column
touched — so this revision is deploy-order free: it can be applied before
or after the code that reads it, and rolling the code back does not
require rolling this back.

**This file was edited in place before it was merged** — twice: a ``title``
column was dropped from ``schedule``, and ``ck_schedule_day_of_month_range``
was widened from 1-28 to 1-31. If your database applied an earlier copy of
this same revision id, alembic will not re-run it — the id is already in
``alembic_version``, so ``upgrade head`` is a no-op — and the stale
``title NOT NULL`` column stays behind, failing every INSERT the current
code makes:

    (psycopg.errors.NotNullViolation) null value in column "title" of
    relation "schedule" violates not-null constraint

The day-of-month edit fails the same way from the other direction: a
database still carrying the 1-28 constraint accepts nothing above 28, while
the current schema layer validates up to 31 — so creating a monthly schedule
on the 29th through 31st passes validation and is refused by Postgres.

Nothing announces either at migration time; both surface as a 500 on the
first create or the first dispatch. ``alembic check`` detects the stale
column (it reports ``remove_column ... 'title'``) but **not** the narrower
CHECK, which alembic does not compare. So: run ``alembic check`` and, if it
reports drift, ``ALTER TABLE schedule DROP COLUMN title``; then confirm the
constraint separately with

    SELECT pg_get_constraintdef(oid) FROM pg_constraint
     WHERE conname = 'ck_schedule_day_of_month_range';

and if it still says 28, drop and re-add it with 31.

``schedule_run`` stores no execution status. The outcome lives on the
session the occurrence enqueued and is reached by join; see the model
docstring for why a copy here would be the losing side of a race.

Two of the four enum columns reuse types that already exist
(``session_tier``, ``agent_type``) — referenced with ``create_type=False``
so this revision neither creates nor drops them. Only the two types this
revision introduces are dropped on downgrade.

**hkgai note — the parent differs from upstream on purpose.** Upstream this
revision hangs off ``b7e2d4c8a915``, which alters ``payment_order`` — a table
this branch dropped, so that revision was never taken here. Scheduled tasks
were also left out of the first sync that met this file; when they came
across, it was re-pointed at this branch's head (``f4c8a2b91d63``) with the
upstream **id** kept so the two chains stay comparable. Do not "correct"
``down_revision`` back on a later sync. The in-place-edit warning above is
about databases that applied an early copy upstream; this branch's database
never had any copy of it.

Revision ID: c8b3e5f1a742
Revises: f4c8a2b91d63
Create Date: 2026-09-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8b3e5f1a742"
down_revision: Union[str, Sequence[str], None] = "f4c8a2b91d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Types this revision owns (created here, dropped on downgrade).
_STATUS_NAME = "schedule_status"
_STATUS_VALUES = ("ACTIVE", "PAUSED", "COMPLETED")
_FREQUENCY_NAME = "schedule_frequency"
_FREQUENCY_VALUES = ("ONCE", "DAILY", "WEEKLY", "MONTHLY")

# Types this revision only references. Owned by the session migrations.
_TIER_NAME = "session_tier"
_TIER_VALUES = ("FAST", "STANDARD", "ADVANCED")
_AGENT_TYPE_NAME = "agent_type"
_AGENT_TYPE_VALUES = ("FULL", "LITE")


def _enum(values: tuple[str, ...], name: str) -> postgresql.ENUM:
    """Reference an existing Postgres enum without emitting CREATE TYPE."""
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    # Raw SQL CREATE TYPE, then referenced with create_type=False on the
    # column — the project convention (see f2a4c6e8b1d3_create_recipe).
    op.execute(f"CREATE TYPE {_STATUS_NAME} AS ENUM ({', '.join(repr(v) for v in _STATUS_VALUES)})")
    op.execute(
        f"CREATE TYPE {_FREQUENCY_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _FREQUENCY_VALUES)})"
    )

    op.create_table(
        "schedule",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No ``title``: the prompt is what identifies a schedule, and the
        # session each run creates derives its own name from it.
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", _enum(_STATUS_VALUES, _STATUS_NAME), nullable=False),
        sa.Column("frequency", _enum(_FREQUENCY_VALUES, _FREQUENCY_NAME), nullable=False),
        sa.Column("at_time", sa.Time(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=True),
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("day_of_month", sa.SmallInteger(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("tier", _enum(_TIER_VALUES, _TIER_NAME), nullable=False),
        sa.Column("agent_type", _enum(_AGENT_TYPE_VALUES, _AGENT_TYPE_NAME), nullable=False),
        sa.Column("thinking_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # The dispatcher's only scan. Partial on ACTIVE: paused and completed
    # rows are most of the table on a long-lived deployment and none of
    # them are ever due.
    op.create_index(
        "ix_schedule_due",
        "schedule",
        ["next_run_at"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_schedule_user_id_created_at",
        "schedule",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_check_constraint(
        "ck_schedule_frequency_fields",
        "schedule",
        "(frequency = 'ONCE' AND run_date IS NOT NULL"
        " AND weekday IS NULL AND day_of_month IS NULL)"
        " OR (frequency = 'DAILY' AND run_date IS NULL"
        " AND weekday IS NULL AND day_of_month IS NULL)"
        " OR (frequency = 'WEEKLY' AND run_date IS NULL"
        " AND weekday IS NOT NULL AND day_of_month IS NULL)"
        " OR (frequency = 'MONTHLY' AND run_date IS NULL"
        " AND weekday IS NULL AND day_of_month IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_schedule_weekday_range", "schedule", "weekday IS NULL OR (weekday BETWEEN 0 AND 6)"
    )
    # 1..31. A month too short for the chosen day does not fire — the cron
    # reading of "the 31st"; ``_first_firing_date`` steps past such months.
    op.create_check_constraint(
        "ck_schedule_day_of_month_range",
        "schedule",
        "day_of_month IS NULL OR (day_of_month BETWEEN 1 AND 31)",
    )

    op.create_table(
        "schedule_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "schedule_id",
            sa.Integer(),
            sa.ForeignKey("schedule.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # The idempotency key: two dispatchers racing the same occurrence, or
    # one retrying after a crash, collide here rather than running the
    # user's task twice.
    op.create_index(
        "uq_schedule_run_occurrence",
        "schedule_run",
        ["schedule_id", "scheduled_for"],
        unique=True,
    )
    op.create_index(
        "ix_schedule_run_unread",
        "schedule_run",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "ix_schedule_run_schedule_id_created_at",
        "schedule_run",
        ["schedule_id", sa.text("created_at DESC")],
    )
    op.create_check_constraint(
        "ck_schedule_run_outcome",
        "schedule_run",
        "NOT (session_id IS NOT NULL AND skip_reason IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_schedule_run_outcome", "schedule_run", type_="check")
    op.drop_index("ix_schedule_run_schedule_id_created_at", table_name="schedule_run")
    op.drop_index("ix_schedule_run_unread", table_name="schedule_run")
    op.drop_index("uq_schedule_run_occurrence", table_name="schedule_run")
    op.drop_table("schedule_run")

    op.drop_constraint("ck_schedule_day_of_month_range", "schedule", type_="check")
    op.drop_constraint("ck_schedule_weekday_range", "schedule", type_="check")
    op.drop_constraint("ck_schedule_frequency_fields", "schedule", type_="check")
    op.drop_index("ix_schedule_user_id_created_at", table_name="schedule")
    op.drop_index("ix_schedule_due", table_name="schedule")
    op.drop_table("schedule")

    # Only the types this revision created. ``session_tier`` / ``agent_type``
    # belong to the session migrations and are still in use.
    op.execute(f"DROP TYPE IF EXISTS {_FREQUENCY_NAME}")
    op.execute(f"DROP TYPE IF EXISTS {_STATUS_NAME}")
