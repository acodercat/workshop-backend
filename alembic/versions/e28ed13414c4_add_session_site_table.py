"""add session_site table

One row per static site published via the worker's ``publish_site``
tool. ``slug`` is the unguessable publish identifier — both the
subdomain label (``https://{slug}.{preview_domain}/``) and the OSS key
prefix — so it's UNIQUE. The worker inserts a row right after the
site's files land in the preview bucket (see
``agent-worker/agent_worker/infra/session_site_repository.py``); the
backend owns the schema.

Indexes:
- ``ix_session_site_user_id_created_at`` — backs the per-user "my
  published sites" listing (filter by owner, newest first).
- ``ix_session_site_session_id`` — Postgres doesn't auto-index FK
  columns; without it a CASCADE delete on the parent session would
  seq-scan this table.

``user_id`` is denormalized from ``session.user_id`` (immutable owner ⇒
never stale) and nullable to mirror it.

Revision ID: e28ed13414c4
Revises: d8c2f4a6e9b1
Create Date: 2026-06-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e28ed13414c4'
down_revision: Union[str, Sequence[str], None] = 'd8c2f4a6e9b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'session_site',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('slug', sa.String(length=63), nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column(
            'created_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['session_id'], ['session.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index(
        'ix_session_site_user_id_created_at',
        'session_site',
        ['user_id', sa.literal_column('created_at DESC')],
    )
    op.create_index(
        'ix_session_site_session_id',
        'session_site',
        ['session_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_session_site_session_id', table_name='session_site')
    op.drop_index('ix_session_site_user_id_created_at', table_name='session_site')
    op.drop_table('session_site')
