"""session_turn.mcp_servers: per-turn snapshot of the run's connected Connectors

Adds the enqueue-time snapshot (``{id, name}`` per Connector) the worker turns
back into live config at connect time. It caps the set — a Connector connected
after enqueue cannot join a queued turn — but it is not a freeze: the worker
re-reads each row by id, deliberately, so a rotated credential or a renamed
server takes effect, and one disconnected between enqueue and claim drops out.

Defaults to an empty JSONB array so every existing/in-flight turn reads as
"no Connectors".

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
