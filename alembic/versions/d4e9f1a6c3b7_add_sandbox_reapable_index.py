"""add partial index on sandbox for the reaper's reapable scan

Backs the sandbox reaper's ``select_reapable``:
``WHERE status <> 'TERMINATED' AND last_used_at < now() - interval
ORDER BY last_used_at``. The only pre-existing index is on
``session_id`` (for ``claim_next_turn``), which does nothing for this
query — so the reaper seq-scans + sorts the whole ``sandbox`` table
every sweep, and that table grows unbounded because ``TERMINATED`` rows
are never deleted.

A *partial* index on ``last_used_at WHERE status <> 'TERMINATED'`` only
covers the live set (a tiny fraction of the table), staying small as
dead rows accumulate, and serves the predicate, the range, and the
ordering in one.

Built ``CONCURRENTLY`` so it doesn't lock writes on a populated
``sandbox`` table — that requires running outside Alembic's wrapping
transaction, hence the ``autocommit_block``.

Revision ID: d4e9f1a6c3b7
Revises: 989ed8bb4430
Create Date: 2026-05-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e9f1a6c3b7"
down_revision: Union[str, Sequence[str], None] = "989ed8bb4430"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_sandbox_reapable"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "sandbox",
            ["last_used_at"],
            postgresql_concurrently=True,
            postgresql_where=sa.text("status <> 'TERMINATED'"),
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="sandbox",
            postgresql_concurrently=True,
            if_exists=True,
        )
