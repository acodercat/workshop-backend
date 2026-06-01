"""session_turn_status: add CANCELLED terminal value

The ``session_turn_status`` enum had four values
(``PENDING`` / ``RUNNING`` / ``COMPLETE`` / ``FAILED``). User-initiated
cancellation was recorded only on the parent ``session.status``
(``CANCELLED``); the ``session_turn`` row itself landed on ``COMPLETE``
because the worker's ``_terminate_turn`` hard-coded it, shared between
the complete and cancel paths. That made a cancelled turn
indistinguishable from a natural completion at the queue layer.

Adding ``CANCELLED`` as a fifth terminal value lets the worker record
the user's intent on the turn row too, mirroring the parent
``session_status`` (which already carries ``CANCELLED`` — see
``c4f8a1d3e9b2``). Billing is unaffected: credit settle reads token
usage off the turn row by id and gates on ``session.status``, never on
``session_turn.status``.

``ALTER TYPE ... ADD VALUE`` is non-locking on Postgres 12+ (transaction-
safe; the new label cannot be USED in the same transaction, but no DML
in this migration uses it). Idempotent via ``IF NOT EXISTS`` so
re-running on a partially-applied environment is safe.

Postgres has no ``ALTER TYPE ... DROP VALUE``, so downgrade can't
strip the label. Documented as one-way.

Revision ID: b2e9c4f7a1d6
Revises: f3b6d1e8a2c7
Create Date: 2026-05-31

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b2e9c4f7a1d6"
down_revision: Union[str, Sequence[str], None] = "f3b6d1e8a2c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE session_turn_status ADD VALUE IF NOT EXISTS 'CANCELLED'"
    )


def downgrade() -> None:
    # Postgres provides no ``ALTER TYPE ... DROP VALUE``. The only way
    # to revert is to drop the type and recreate it without the label,
    # which requires moving every row off the column first. Out of scope
    # for an automated downgrade — if a rollback is genuinely needed,
    # the operator should do it manually with a session-turn-status cast
    # plan and verify no rows hold the value being dropped.
    raise NotImplementedError(
        "Postgres cannot drop enum values; manual schema migration required"
    )
