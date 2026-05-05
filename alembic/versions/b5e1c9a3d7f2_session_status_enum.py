"""session.status: varchar(16) → Postgres enum (UPPERCASE values)

Aligns ``session.status`` with the project enum convention used by
``sandbox.status`` (``sandbox_status``), ``message.role``
(``message_role``), and ``session_run.status`` (``session_run_status``):
typed Postgres enum + UPPERCASE labels matching the Python member
names.

Three-step conversion under one transaction:

1. ``CREATE TYPE session_status AS ENUM ('RUNNING', 'COMPLETE', 'FAILED')``
2. ``UPDATE session SET status = upper(status)`` so existing rows have
   labels the new enum can hold.
3. ``ALTER COLUMN ... TYPE session_status USING ...::text::session_status``
   plus a re-set of the server_default (Postgres can't auto-cast a
   ``'complete'::varchar`` default into the new enum in the same
   ALTER, so it's dropped + reinstated as a typed literal).

Revision ID: b5e1c9a3d7f2
Revises: a8e4f2c6b9d3
Create Date: 2026-05-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5e1c9a3d7f2"
down_revision: Union[str, Sequence[str], None] = "a8e4f2c6b9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VALUES = ("RUNNING", "COMPLETE", "FAILED")
_ENUM_NAME = "session_status"


def upgrade() -> None:
    op.execute(
        f"CREATE TYPE {_ENUM_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _VALUES)})"
    )

    # Existing rows are lowercase varchar — uppercase before the cast so
    # the USING clause has valid enum labels to map to.
    op.execute("UPDATE session SET status = upper(status)")

    # Drop the server_default before altering the column type — Postgres
    # can't cast a ``'complete'::varchar`` default into ``session_status``
    # in the same statement that introduces the enum.
    op.alter_column("session", "status", server_default=None)

    op.alter_column(
        "session",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False),
        existing_nullable=False,
        postgresql_using=f"status::text::{_ENUM_NAME}",
    )

    op.alter_column(
        "session",
        "status",
        server_default=sa.text(f"'COMPLETE'::{_ENUM_NAME}"),
    )


def downgrade() -> None:
    op.alter_column("session", "status", server_default=None)

    op.alter_column(
        "session",
        "status",
        existing_type=sa.Enum(*_VALUES, name=_ENUM_NAME),
        type_=sa.String(length=16),
        existing_nullable=False,
        postgresql_using="status::text",
    )

    op.alter_column("session", "status", server_default="complete")

    op.execute("UPDATE session SET status = lower(status)")
    op.execute(f"DROP TYPE IF EXISTS {_ENUM_NAME}")
