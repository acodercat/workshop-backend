"""schedule_run: index user_id for the FK reference check

``ix_schedule_run_unread`` leads on ``user_id`` but carries
``WHERE read_at IS NULL``. A partial index cannot serve the reference check
Postgres runs when a ``user`` row is deleted, so that check was a sequential
scan of ``schedule_run`` — the table in this pair that grows without bound.

``schedule`` never had the problem: its ``ix_schedule_user_id_created_at``
is unpartitioned and already covers the FK. Only this sibling was left bare.

Plain (not ``CONCURRENTLY``) DDL: this lands in the same release as the
table itself, so every deployment applying it has an empty or near-empty
``schedule_run``, and the brief ``SHARE`` lock costs nothing worth an
``autocommit_block``. Adding an index to this table once it has accumulated
history is a different decision.

Revision ID: d4a1e8b62f37
Revises: c8b3e5f1a742
Create Date: 2026-09-11

"""

from typing import Sequence, Union

from alembic import op

revision: str = "d4a1e8b62f37"
down_revision: Union[str, Sequence[str], None] = "c8b3e5f1a742"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_schedule_run_user_id", "schedule_run", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_schedule_run_user_id", table_name="schedule_run")
