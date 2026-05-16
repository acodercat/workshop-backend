"""session soft-delete: add deleted_at + make user/updated_at index partial

``DELETE /sessions/{id}`` becomes a soft-delete: the row is retained
(billing/audit trail, credit_reservation back-link preserved) and just
filtered out of the user's view via ``deleted_at IS NULL``.

- ``deleted_at`` is nullable with no default → ``ADD COLUMN`` is an
  instant catalog-only change, no table rewrite.
- ``ix_session_user_id_updated_at`` is swapped to a **partial** index
  (``WHERE deleted_at IS NULL``). Its only consumer is the
  active-sessions list query, which always carries that predicate, so
  the partial form is a strict superset-free match and stays smaller.
  Built ``CONCURRENTLY`` inside ``autocommit_block`` (mirrors
  ``b74c1e2f9a30``) so the swap doesn't take a write-blocking lock on
  a populated ``session`` table.

Revision ID: a7e3f1b9d6c4
Revises: f5d2c8a1e9b4
Create Date: 2026-05-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7e3f1b9d6c4"
down_revision: Union[str, Sequence[str], None] = "f5d2c8a1e9b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_session_user_id_updated_at"


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="session",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            _INDEX_NAME,
            "session",
            ["user_id", sa.text("updated_at DESC")],
            postgresql_concurrently=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
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
        op.create_index(
            _INDEX_NAME,
            "session",
            ["user_id", sa.text("updated_at DESC")],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
    op.drop_column("session", "deleted_at")
