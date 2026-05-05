"""session_status: add IDLE initial-state value

Adds ``IDLE`` to the ``session_status`` enum. A freshly-created session
that hasn't had any turn queued yet has no honest place in the existing
four-value enum (``RUNNING``/``COMPLETE``/``CANCELLED``/``FAILED`` each
describe a state that did happen). ``IDLE`` is what every new row
lands in until the backend's ``queue_user_message`` flips it to
``RUNNING`` on the first turn.

Pure ``ADD VALUE`` — the column default is dropped in a separate
follow-up migration (``e9b3a5c7d2f8``) so the two changes can be
deployed independently:

  1. Apply this migration → no behaviour change for old code (the
     prior default ``COMPLETE`` keeps working) but new code that sets
     ``status=IDLE`` can now succeed.
  2. Deploy backend code that sets ``status=IDLE`` explicitly.
  3. Apply ``e9b3a5c7d2f8`` to drop the default → forces explicit set,
     locking in the new contract.

``ALTER TYPE ... ADD VALUE`` is non-locking and transaction-safe on
Postgres 12+; idempotent via ``IF NOT EXISTS``. Postgres has no
``DROP VALUE``, so this is one-way — downgrade raises (consistent
with the CANCELLED migration ``c4f8a1d3e9b2``).

Revision ID: d7a2e8b5f6c1
Revises: c4f8a1d3e9b2
Create Date: 2026-05-05

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d7a2e8b5f6c1"
down_revision: Union[str, Sequence[str], None] = "c4f8a1d3e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE session_status ADD VALUE IF NOT EXISTS 'IDLE'"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Postgres cannot drop enum values; manual schema migration required"
    )
