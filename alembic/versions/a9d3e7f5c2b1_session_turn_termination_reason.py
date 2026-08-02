"""session_turn.termination_reason — why a turn reached its terminal status

The *why* to ``status``'s *what*: same CANCELLED can be the user's stop
button, the budget enforcer, or a worker drain; same FAILED can be a model
error, a hard timeout, or a lost lease. ``status`` keeps driving billing and
recovery; this column is the persisted, queryable reason (UI copy +
operations analytics). Plain VARCHAR — descriptive, never load-bearing, so
no native enum / CHECK (a new reason must not cost an ``ALTER TYPE``).

Backfill writes only the values that are *certain* from history:

- ``COMPLETE`` → ``completed`` (that's what COMPLETE means).
- ``CANCELLED`` → ``user_cancelled`` (the enforcer didn't exist yet; every
  historical cancel was the user's).
- ``FAILED`` stays NULL — the sub-reason (model_error / timeout / lost
  lease / …) is unknowable after the fact; a guessed value would poison
  analytics. NULL = "pre-rollout, reason unrecorded".

Revision ID: a9d3e7f5c2b1
Revises: f2b8d6c4a1e7
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a9d3e7f5c2b1"
down_revision: Union[str, Sequence[str], None] = "f2b8d6c4a1e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_turn",
        sa.Column("termination_reason", sa.String(length=32), nullable=True),
    )
    op.execute(
        """
        UPDATE session_turn
           SET termination_reason = CASE status
                                        WHEN 'COMPLETE'  THEN 'completed'
                                        WHEN 'CANCELLED' THEN 'user_cancelled'
                                    END
         WHERE status IN ('COMPLETE', 'CANCELLED')
        """
    )


def downgrade() -> None:
    op.drop_column("session_turn", "termination_reason")
