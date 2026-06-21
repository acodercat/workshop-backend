"""add sandbox_status 'REAPING'

Introduces an intermediate ``REAPING`` state between ``RUNNING`` and
``TERMINATED`` so the sandbox reaper can *claim* a row before it kills
the container. Without it the reaper selects an idle row, releases its
lock, and only then kills — a window in which a new turn can reconnect
to the same container (bumping ``last_used_at``) and have it killed
out from under it. ``claim_for_reap`` flips RUNNING/PAUSED/REAPING →
REAPING under a re-asserted idle predicate; the worker's
``claim_next_turn`` excludes REAPING so a row mid-reap is never handed
to a new turn.

Adding a value to a Postgres enum cannot run inside the transaction
alembic wraps each migration in (on pre-12 servers, and is finicky
elsewhere), so it runs in an autocommit block. ``IF NOT EXISTS`` keeps
the migration idempotent across re-runs.

Revision ID: a1c4e7b2f9d3
Revises: b4e2d8f1a9c3
Create Date: 2026-06-11

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a1c4e7b2f9d3"
down_revision: Union[str, Sequence[str], None] = "b4e2d8f1a9c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE sandbox_status ADD VALUE IF NOT EXISTS 'REAPING'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type without recreating
    # the type and rewriting every dependent column — not worth it for a
    # transient lifecycle state. Leaving the value in place is harmless:
    # nothing writes REAPING once the worker code is rolled back, and the
    # reaper's selection predicate (``status <> 'TERMINATED'``) still works.
    pass
