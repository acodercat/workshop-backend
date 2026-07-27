"""session_turn.mcp_servers: per-turn snapshot of the run's enabled Connectors

Adds the enqueue-time snapshot (id/name/... per Connector) the worker resolves
at connect time, so a later ``mcp_server`` row edit can't change which servers
an already-queued turn connects. Defaults to an empty JSONB array so every
existing/in-flight turn reads as "no Connectors".

Revision ID: b7e4d9a2c5f1
Revises: a3f8c2d6e1b4
Create Date: 2026-07-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7e4d9a2c5f1"
down_revision: Union[str, Sequence[str], None] = "a3f8c2d6e1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_turn",
        sa.Column(
            "mcp_servers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_turn", "mcp_servers")
