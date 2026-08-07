"""session.mcp_server_ids — Connectors are session configuration

The composer switched Connectors to a Switch, and a Switch means "from now
on". Nothing in the schema said so: the ids rode in on every
``POST /sessions/{id}/messages`` and lived only in the composer's React
state, so they were lost on send, on refresh and in any other tab.

They belong beside ``tier`` and ``thinking_enabled`` — declared once,
applied to every turn. ``session_turn.mcp_servers`` is untouched and still
written per turn: this column is the current intent, that one the record of
what a turn actually got.

Backfills to ``'[]'``, which is what every existing session effectively had.

Revision ID: f6a3c9d1e482
Revises: a9c4e2f7d1b8
Create Date: 2026-08-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f6a3c9d1e482"
down_revision: Union[str, Sequence[str], None] = "a9c4e2f7d1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with a server default is metadata-only on PG 11+ — no table
    # rewrite.
    op.add_column(
        "session",
        sa.Column(
            "mcp_server_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("session", "mcp_server_ids")
