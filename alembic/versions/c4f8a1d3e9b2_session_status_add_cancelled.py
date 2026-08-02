"""session_status: add CANCELLED terminal value

The ``session_status`` enum previously had three values
(``RUNNING`` / ``COMPLETE`` / ``FAILED``). User-initiated cancellation
was indistinguishable from a natural completion in cold-loaded data —
the worker hard-coded ``COMPLETE`` regardless of axon's
``stop_reason="cancelled"``, so the cancel signal evaporated the
moment the SSE stream closed.

Adding ``CANCELLED`` as a fourth terminal value lets the worker
record the user's intent honestly, so reloads, sidebar listings, and
analytics can all see "this session was cancelled" rather than
guessing from a truncated assistant reply.

``ALTER TYPE ... ADD VALUE`` is non-locking on Postgres 12+ (transaction-
safe; the new label cannot be USED in the same transaction — hence the
autocommit block, since Alembic runs a whole ``upgrade`` in one — but no DML
in this migration uses it). Idempotent via ``IF NOT EXISTS`` so
re-running on a partially-applied environment is safe.

Postgres has no ``ALTER TYPE ... DROP VALUE``, so downgrade can't
strip the label. Documented as one-way.

Revision ID: c4f8a1d3e9b2
Revises: 9393b13c8874
Create Date: 2026-05-05

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c4f8a1d3e9b2"
down_revision: Union[str, Sequence[str], None] = "9393b13c8874"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Committed on its own: Postgres refuses to *use* a value added
        # in the still-open transaction, and Alembic runs a whole
        # ``upgrade`` in one. A later revision in the same run naming
        # this label — a CHECK constraint, a backfill — would fail
        # without this, and which revision that is, is not something
        # this one can know.
        op.execute(
        "ALTER TYPE session_status ADD VALUE IF NOT EXISTS 'CANCELLED'"
        )


def downgrade() -> None:
    # Postgres provides no ``ALTER TYPE ... DROP VALUE``. The only way
    # to revert is to drop the type and recreate it without the label,
    # which requires moving every row off the column first. Out of scope
    # for an automated downgrade — if a rollback is genuinely needed,
    # the operator should do it manually with a session-status cast
    # plan and verify no rows hold the value being dropped.
    raise NotImplementedError(
        "Postgres cannot drop enum values; manual schema migration required"
    )
