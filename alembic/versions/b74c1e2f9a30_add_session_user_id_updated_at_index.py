"""add (user_id, updated_at DESC) index on session

Backs the hot path ``GET /sessions``: filter by ``user_id`` then
``ORDER BY updated_at DESC``. Postgres does not auto-create indexes
on foreign keys, so without this both the page query and its count
sibling fall back to seq scans.

Built ``CONCURRENTLY`` so it doesn't lock writes on a populated
``session`` table — that requires running outside Alembic's wrapping
transaction, hence the ``autocommit_block``.

Revision ID: b74c1e2f9a30
Revises: 004dd48dc4a1
Create Date: 2026-04-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b74c1e2f9a30"
down_revision: Union[str, Sequence[str], None] = "004dd48dc4a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_session_user_id_updated_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "session",
            ["user_id", sa.text("updated_at DESC")],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="session",
            postgresql_concurrently=True,
            if_exists=True,
        )
