"""index session.updated_at for the platform-wide session list

``GET /sessions`` (the console's cross-user list) pages every session
newest-activity-first: ``ORDER BY updated_at DESC, id DESC LIMIT n``,
soft-deleted rows included.

``ix_session_user_id_updated_at`` cannot serve that. It leads with
``user_id`` — useless to a query that filters on no user — and it is
partial on ``deleted_at IS NULL``, while this list deliberately shows
the deleted ones support is asked about.

Without this index the page is a sequential scan of ``session`` plus a
top-N sort of every row in it, on a table that gains a row per
conversation, forever. With it the twenty rows are read straight off
the index in order.

Both columns descend so the index order matches the query's exactly; a
mixed-direction index would still be usable, but only backwards, and
only for one of the two keys.

Built ``CONCURRENTLY``: ``session`` is written on every turn, and a
plain ``CREATE INDEX`` would hold a ``SHARE`` lock against those writes
for the whole build. That requires running outside Alembic's wrapping
transaction, hence the ``autocommit_block`` — the same shape
``e7b1d4a9c2f6`` uses on ``credit_ledger``.

Revision ID: f4c8a2b91d63
Revises: d5b1e93f7a48
Create Date: 2026-09-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4c8a2b91d63"
down_revision: Union[str, Sequence[str], None] = "d5b1e93f7a48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_session_updated_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "session",
            [sa.text("updated_at DESC"), sa.text("id DESC")],
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
