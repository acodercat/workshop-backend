"""queued_message.steer, skill_snapshots and steered_turn_id: steering a running turn

A waiting message the owner steers with goes into the running turn instead
of waiting for it to end. ``steer`` is the request, false for every existing
row; ``skill_snapshots`` the message's skills resolved then, empty for every
existing row; ``steered_turn_id`` the turn that took the message, NULL until
one does.

Revision ID: e1f4a7c2b9d3
Revises: 9bd99073dc16
Create Date: 2026-11-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f4a7c2b9d3"
down_revision: Union[str, Sequence[str], None] = "9bd99073dc16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "queued_message",
        sa.Column("steer", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "queued_message",
        sa.Column(
            "skill_snapshots",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("queued_message", sa.Column("steered_turn_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "queued_message_steered_turn_id_fkey",
        "queued_message",
        "session_turn",
        ["steered_turn_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("queued_message_steered_turn_id_fkey", "queued_message", type_="foreignkey")
    op.drop_column("queued_message", "steered_turn_id")
    op.drop_column("queued_message", "skill_snapshots")
    op.drop_column("queued_message", "steer")
