"""session_turn.reminders: per-turn system reminders rendered by the backend

Model-facing context that belongs to one turn but is not the user's text —
today, a scheduled run telling the model nobody is there to answer
questions, and which zone and instant "today" means. The worker appends
each string to the turn's ``Agent.run(reminders=...)`` verbatim.

Plain text rather than a structured snapshot, so the worker needs no
knowledge of the feature that wrote it. ``NOT NULL DEFAULT '[]'`` like
``skills`` and ``mcp_servers``: readers iterate without a null-guard, and a
backend process still running the previous code keeps inserting turns.

**Deploy order.** The worker selects this column when it claims a turn, so
this revision must be applied before a worker that reads it starts —
otherwise every claim fails with ``UndefinedColumn``.

Revision ID: c4cba460bb50
Revises: a9e4c2d71f38
Create Date: 2026-09-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4cba460bb50"
down_revision: Union[str, Sequence[str], None] = "a9e4c2d71f38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_turn",
        sa.Column(
            "reminders",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_turn", "reminders")
