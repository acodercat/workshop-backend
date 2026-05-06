"""add session_share table

Backs the public session-share feature. One row per share link;
revocation is a real ``DELETE`` (no soft-delete flag) — at most one
share per session under current policy.

Indexes:
- ``ix_session_share_token`` UNIQUE — primary lookup path (visitor pastes
  token in URL, backend resolves to share row).
- ``ix_session_share_session_id`` — owner-side lookup ("the share
  for this session"). Plain (non-partial) since hard-delete keeps
  the table small.

CASCADE on the session FK: deleting a session takes its share with it.
The owner-side endpoint refuses to delete RUNNING sessions, so a share
can never outlive the active turn it points at.

Revision ID: 3e1329e0b598
Revises: e9b3a5c7d2f8
Create Date: 2026-05-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3e1329e0b598'
down_revision: Union[str, Sequence[str], None] = 'e9b3a5c7d2f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'session_share',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('token', sa.String(length=43), nullable=False),
        sa.Column('password_hash', sa.String(length=72), nullable=True),
        sa.Column(
            'created_at',
            sa.TIMESTAMP(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['session_id'], ['session.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_session_share_session_id', 'session_share', ['session_id'],
    )
    op.create_index(
        op.f('ix_session_share_token'),
        'session_share',
        ['token'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_session_share_token'), table_name='session_share')
    op.drop_index('ix_session_share_session_id', table_name='session_share')
    op.drop_table('session_share')
