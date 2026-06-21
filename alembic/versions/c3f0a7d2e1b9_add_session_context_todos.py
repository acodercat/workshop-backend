"""add session_context.todos

The session's durable todo checklist. ``todo_write`` is compactable in axon,
so its calls can be summarized out of ``session_context.messages`` — this
separate column is never touched by compaction. The worker rewrites it each
turn from ``Agent.todos`` and seeds the next run via ``Agent.run(todos=...)``.
Existing rows backfill to the empty list via the ``server_default``.

Revision ID: c3f0a7d2e1b9
Revises: e7b1d4a9c2f6
Create Date: 2026-06-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f0a7d2e1b9"
down_revision: Union[str, Sequence[str], None] = "e7b1d4a9c2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_context",
        sa.Column(
            "todos",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_context", "todos")
