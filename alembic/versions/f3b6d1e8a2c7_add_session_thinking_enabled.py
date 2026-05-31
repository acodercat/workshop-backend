"""add session.thinking_enabled flag

Per-session agent-thinking toggle, set at creation from the composer switch
(default on). Surfaced to users as "deep think". The worker reads it at
claim time to toggle the chosen model's thinking config; like ``model_id`` /
``tier`` it is never mutated after creation.

Existing rows predate the flag, so the column is added NOT NULL with a
temporary ``server_default false`` to backfill, then the default is dropped
— matching ``session.tier`` / ``session.agent_type`` (no column default,
set explicitly at ``create_session``).

Revision ID: f3b6d1e8a2c7
Revises: e7a2f0c5b9d4
Create Date: 2026-05-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3b6d1e8a2c7"
down_revision: Union[str, Sequence[str], None] = "e7a2f0c5b9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column(
            "thinking_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("session", "thinking_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("session", "thinking_enabled")
