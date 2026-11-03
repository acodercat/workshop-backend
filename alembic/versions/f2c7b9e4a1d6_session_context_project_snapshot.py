"""session_context.project_snapshot: what the agent was last told of its project

The worker writes it with the agent's memory, in the fenced terminal
write, and reads it at claim to tell the agent only what changed in its
project since the last turn. Nullable with no default: a catalog-only
change, no rewrite of ``session_context``.

Revision ID: f2c7b9e4a1d6
Revises: d3a8f5c1e7b2
Create Date: 2026-11-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2c7b9e4a1d6"
down_revision: Union[str, Sequence[str], None] = "d3a8f5c1e7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_context",
        sa.Column("project_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_context", "project_snapshot")
