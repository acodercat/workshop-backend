"""schedule: per-task skill picks and Connector bindings

A scheduled task could only ever send a prompt. Two additions let it carry
the same context an ordinary message can:

``schedule.skills`` — the names the owner picked, re-resolved at every
firing rather than snapshotted. Since ``core/agent-worker`` narrowed the
model's listing to live ``SYSTEM`` skills plus the turn's own picks, this
column is the *only* way an ``OFFICIAL`` or ``CUSTOM`` skill can reach a
scheduled run at all; without it such a run sees none of them.

``schedule_mcp_server`` — a table rather than an id array, for the reason
its ``session_mcp_server`` twin is one: ``ON DELETE CASCADE`` from
``mcp_server`` has to unbind a deleted Connector from every schedule that
named it, which an array cannot express.

Both are additive and default to empty, so existing schedules keep
behaving exactly as they did. Plain (not ``CONCURRENTLY``) DDL: the new
column takes a non-volatile default, which Postgres 11+ applies without
rewriting the table, and ``schedule`` is small besides.

Revision ID: b5e9d3f70a26
Revises: a7d2f4b9c613
Create Date: 2026-09-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5e9d3f70a26"
down_revision: Union[str, Sequence[str], None] = "a7d2f4b9c613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schedule",
        sa.Column(
            "skills",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_table(
        "schedule_mcp_server",
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("mcp_server_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_server.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("schedule_id", "mcp_server_id"),
    )
    # The composite PK leads with ``schedule_id``, so it cannot serve the
    # ``ON DELETE CASCADE`` from ``mcp_server``. Without this, deleting a
    # Connector seq-scans this table.
    op.create_index(
        "ix_schedule_mcp_server_mcp_server_id",
        "schedule_mcp_server",
        ["mcp_server_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_schedule_mcp_server_mcp_server_id", table_name="schedule_mcp_server")
    op.drop_table("schedule_mcp_server")
    op.drop_column("schedule", "skills")
