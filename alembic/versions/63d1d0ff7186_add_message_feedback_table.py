"""add message_feedback table

Backs the per-message thumbs-up / thumbs-down feature. One row =
one user's feedback on one message.

Constraints:
- ``UNIQUE (message_id, user_id)`` — one vote per user per message,
  flipping rating or editing the comment upserts in place. Doubles as
  the read-path index for "did I vote on this message".
- ``CHECK (rating IN (1, -1))`` — binary vote. 1 = up, -1 = down.
  Stored numerically so the check is a one-liner; the API surface
  translates to/from ``"up" / "down"`` strings.
- ``ix_message_feedback_message_id`` — Postgres doesn't auto-index FK
  columns; without this, CASCADE delete on the parent message would
  seq-scan ``message_feedback``.

CASCADE on both FKs: deleting a message or a user takes their
feedback rows with them — no orphan rows worth keeping.

Revision ID: 63d1d0ff7186
Revises: f681f1d56d1c
Create Date: 2026-05-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63d1d0ff7186'
down_revision: Union[str, Sequence[str], None] = 'f681f1d56d1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'message_feedback',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('rating', sa.SmallInteger(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.TIMESTAMP(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.TIMESTAMP(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint(
            'rating IN (1, -1)', name='ck_message_feedback_rating',
        ),
        sa.ForeignKeyConstraint(
            ['message_id'], ['message.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['user.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'message_id', 'user_id', name='uq_message_feedback_user',
        ),
    )
    op.create_index(
        'ix_message_feedback_message_id',
        'message_feedback',
        ['message_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_message_feedback_message_id', table_name='message_feedback',
    )
    op.drop_table('message_feedback')
