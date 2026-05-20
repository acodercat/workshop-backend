"""session_turn: add terminated_at

Records the wall-clock moment a turn was flipped to a terminal status
(COMPLETE / CANCELLED / FAILED) by its writer (worker terminal write
or recovery cron). Distinct from ``updated_at`` — which is bumped by
every UPDATE, including lease-renew heartbeats — so duration math
that anchors on this column survives any future write path being added.

Nullable + no server default → instant catalog-only ``ADD COLUMN``,
no table rewrite; safe on a live ``session_turn`` table.

Revision ID: e8b2f4d7c1a9
Revises: d3a7f1c9b6e2
Create Date: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8b2f4d7c1a9"
down_revision: Union[str, Sequence[str], None] = "d3a7f1c9b6e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_turn",
        sa.Column("terminated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_turn", "terminated_at")
