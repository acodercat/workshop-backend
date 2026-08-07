"""session_mcp_server — the Connector configuration becomes a table

``f6a3c9d1e482`` put it on ``session`` as a JSONB id array, copying the shape
of ``session_turn.mcp_servers`` next door. Wrong precedent: that column is an
immutable snapshot of what one turn got, and JSONB is right there *because* a
later deletion must not rewrite history. This one points at live rows.

Without a foreign key, deleting a Connector left its id in every session that
had it configured — harmless at enqueue, which re-checks ownership, but stale
data that every future deletion path would have to remember to clean. The
cascade here is the database remembering instead.

Replaces rather than rewrites that revision: it is already pushed, so an
installation may have applied it. Dropping the column here is correct whether
it was applied minutes ago or not at all — Alembic runs both in order.

Nothing is carried across. The column shipped in the same batch as this and
never reached a client that could write to it; anything already in it would be
a test row.

Revision ID: c1e7a4b2f983
Revises: f6a3c9d1e482
Create Date: 2026-08-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1e7a4b2f983"
down_revision: Union[str, Sequence[str], None] = "f6a3c9d1e482"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_mcp_server",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"], ["mcp_server.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("session_id", "mcp_server_id"),
    )
    # The composite PK leads with ``session_id``, so it cannot serve the cascade
    # from ``mcp_server`` — the deletion path this table exists for.
    op.create_index(
        "ix_session_mcp_server_mcp_server_id",
        "session_mcp_server",
        ["mcp_server_id"],
    )
    op.drop_column("session", "mcp_server_ids")


def downgrade() -> None:
    op.add_column(
        "session",
        sa.Column(
            "mcp_server_ids",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_index(
        "ix_session_mcp_server_mcp_server_id", table_name="session_mcp_server"
    )
    op.drop_table("session_mcp_server")
