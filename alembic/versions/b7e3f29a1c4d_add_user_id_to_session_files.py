"""add user_id to session_files

Denormalizes the owning user onto ``session_files``, mirroring
``session_site.user_id``: a nullable, ``ON DELETE CASCADE`` FK to ``user``
(anonymous sessions carry no owner, so it mirrors ``session.user_id``'s
nullability), copied from the producing session at write time. The owner
relationship is immutable, so this copy can never go stale. CASCADE
matches the rest of the owned-content user FKs (a sibling migration flips
``session`` / ``session_site`` to CASCADE too).

This only lays the column + index; it changes no queries. With it in
place the per-user file reads (the "my files" library, the
``max_session_files_per_user`` / ``..._bytes_per_user`` quota counts)
can later scan ``session_files`` by owner instead of joining ``session``.

Steps:
1. Add the nullable column (catalog-only — no server default ⇒ no table
   rewrite) + the FK to ``user``.
2. Backfill the owner from each row's session in one pass. Anonymous
   sessions leave ``user_id`` NULL. (If ``session_files`` is very large,
   run this backfill as a batched script instead.)
3. Index ``(user_id, created_at DESC)`` for the per-user listing /
   counting, matching ``ix_session_site_user_id_created_at``.

Revision ID: b7e3f29a1c4d
Revises: 90afe547ccf4
Create Date: 2026-06-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3f29a1c4d'
down_revision: Union[str, Sequence[str], None] = '90afe547ccf4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'session_files',
        sa.Column('user_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'session_files_user_id_fkey',
        'session_files',
        'user',
        ['user_id'],
        ['id'],
        ondelete='CASCADE',
    )
    # Backfill from the producing session. ``IS DISTINCT FROM`` keeps it a
    # no-op on rows already matching, so the statement is safely re-runnable.
    op.execute(
        "UPDATE session_files AS sf "
        "SET user_id = s.user_id "
        "FROM session AS s "
        "WHERE s.id = sf.session_id "
        "AND sf.user_id IS DISTINCT FROM s.user_id"
    )
    op.create_index(
        'ix_session_files_user_id_created_at',
        'session_files',
        ['user_id', sa.literal_column('created_at DESC')],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_session_files_user_id_created_at', table_name='session_files'
    )
    op.drop_constraint(
        'session_files_user_id_fkey', 'session_files', type_='foreignkey'
    )
    op.drop_column('session_files', 'user_id')
