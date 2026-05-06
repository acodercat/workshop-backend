"""add session_files table

Backs the file→session ownership invariant: every file_key the
backend / share endpoints will mint a presigned URL for must have a
row here. Replaces the prior dual authorization paths
(``_parse_outputs_session_id`` for tool-output keys + JSONB scan over
``Message.blocks`` for user uploads), neither of which was schema-
enforced.

Indexes:
- ``ix_session_files_file_key`` UNIQUE — OSS keys are unique by
  construction (uuid components in both formats), so this doubles as
  the primary read-path index.
- ``ix_session_files_session_id`` — Postgres doesn't auto-index FK
  columns; without this CASCADE delete on the parent session would
  seq-scan ``session_files``.

CASCADE on the session FK: deleting a session takes its file rows
with it (the actual OSS objects expire under their lifecycle policy;
this row's job is just authorization).

Backfill: any pre-existing files referenced from message blocks (user
uploads in ``text`` blocks, tool outputs in ``tool_result`` blocks) are
migrated in by a one-shot SQL pass below. ``ON CONFLICT DO NOTHING``
makes it safe to re-run.

Revision ID: f681f1d56d1c
Revises: 3e1329e0b598
Create Date: 2026-05-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f681f1d56d1c'
down_revision: Union[str, Sequence[str], None] = '3e1329e0b598'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'session_files',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('file_key', sa.String(), nullable=False),
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
        op.f('ix_session_files_file_key'),
        'session_files',
        ['file_key'],
        unique=True,
    )
    op.create_index(
        'ix_session_files_session_id',
        'session_files',
        ['session_id'],
    )

    # Backfill from existing data. Walks the JSONB ``blocks`` column on
    # every message, flattens nested ``files`` arrays, and inserts one
    # row per (session_id, file_key). LATERAL keeps the unnesting tied
    # to each message row so the join doesn't explode. ``COALESCE``
    # tolerates blocks without a ``files`` field; the ``f ? 'file_key'``
    # check skips malformed entries. Idempotent via ON CONFLICT.
    op.execute(
        """
        INSERT INTO session_files (session_id, file_key, created_at)
        SELECT m.session_id, f->>'file_key', m.created_at
        FROM message m
             CROSS JOIN LATERAL jsonb_array_elements(m.blocks) AS b
             CROSS JOIN LATERAL
                 jsonb_array_elements(COALESCE(b->'files', '[]'::jsonb)) AS f
        WHERE f ? 'file_key'
        ON CONFLICT (file_key) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_session_files_session_id', table_name='session_files')
    op.drop_index(op.f('ix_session_files_file_key'), table_name='session_files')
    op.drop_table('session_files')
