"""add user_feedback table

Schema documentation lives in :mod:`app.models.user_feedback` —
this migration only creates the table, ``feedback_status`` enum,
and two indexes; it doesn't touch any existing rows.

Revision ID: 712ad01e4590
Revises: 470bec234c01
Create Date: 2026-05-08 16:11:14.111780

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '712ad01e4590'
down_revision: Union[str, Sequence[str], None] = '470bec234c01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_feedback',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('message', sa.String(length=2000), nullable=False),
        sa.Column(
            'image_keys',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            'status',
            sa.Enum('OPEN', 'RESOLVED', 'IGNORED', name='feedback_status'),
            server_default='OPEN',
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_user_feedback_status_created',
        'user_feedback',
        ['status', 'created_at'],
        unique=False,
    )
    op.create_index(
        'ix_user_feedback_user_id',
        'user_feedback',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_user_feedback_user_id', table_name='user_feedback')
    op.drop_index('ix_user_feedback_status_created', table_name='user_feedback')
    op.drop_table('user_feedback')
    # The PG enum type is auto-created by ``sa.Enum`` on first use but
    # not auto-dropped on ``drop_table`` — drop it explicitly so the
    # migration is fully reversible.
    sa.Enum(name='feedback_status').drop(op.get_bind(), checkfirst=True)
