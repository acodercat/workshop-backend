"""drop sandbox.turn_count

The column was bookkeeping that nothing downstream consumed — neither
the backend nor the reaper uses it. ``last_used_at`` already captures
what the reaper needs for idle sweeps; tracking a per-sandbox turn
counter on top of that was noise.

Revision ID: a3e1c02b8f45
Revises: fd72eadcb874
Create Date: 2026-04-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3e1c02b8f45"
down_revision: Union[str, Sequence[str], None] = "fd72eadcb874"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("sandbox", "turn_count")


def downgrade() -> None:
    op.add_column(
        "sandbox",
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
    )
