"""create queued_message: messages sent while a turn runs

A message sent while its session's turn is still running waits in this
table and is sent on, in order, as turns end — by the background process,
so it goes out whether or not the session is open in a browser.

Revision ID: c2f7a9e4b518
Revises: f4d8b2a6c931
Create Date: 2026-11-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c2f7a9e4b518"
down_revision: Union[str, Sequence[str], None] = "f4d8b2a6c931"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "queued_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "files",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "skills",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("held_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_queued_message_session_id", "queued_message", ["session_id", "position"]
    )


def downgrade() -> None:
    op.drop_index("ix_queued_message_session_id", table_name="queued_message")
    op.drop_table("queued_message")
