"""session_status: drop column default

Companion to ``d7a2e8b5f6c1`` (which added the ``IDLE`` enum value).
Now that the model carries no Python-side default and ``create_session``
sets ``status`` explicitly to ``IDLE``, the column-level Postgres
default of ``'COMPLETE'`` is dishonest legacy — a session with zero
turns is not "complete", and the default was only there to satisfy
NOT NULL when no INSERT site bothered to set the column.

Split from the ADD VALUE migration so the two changes can deploy in
the safe order:

  1. ``d7a2e8b5f6c1`` (ADD VALUE 'IDLE') — applied first, no
     behaviour change, opens the door for code that sets IDLE.
  2. Deploy backend code with ``status=SessionStatus.IDLE`` explicit
     in ``create_session``.
  3. This migration (DROP DEFAULT) — applied last, locks in the new
     contract that every INSERT must specify status.

A combined migration would force a single-instant deploy where the
DB schema and the application code change atomically; splitting lets
operators apply the two ALTERs across a rolling deploy without a
window where new schema rejects old code or vice versa.

``DROP DEFAULT`` is metadata only — non-locking, instant, no row
rewrites. Reversible via ``SET DEFAULT 'COMPLETE'`` (the prior
value), so unlike the enum-add migration this one has a meaningful
downgrade.

Revision ID: e9b3a5c7d2f8
Revises: d7a2e8b5f6c1
Create Date: 2026-05-05

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e9b3a5c7d2f8"
down_revision: Union[str, Sequence[str], None] = "d7a2e8b5f6c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE session ALTER COLUMN status DROP DEFAULT")


def downgrade() -> None:
    # Restore the prior column default. Honest reversal of this
    # migration only — the IDLE enum value added by d7a2e8b5f6c1 is
    # one-way (Postgres has no DROP VALUE) and must be rolled back
    # separately if needed (manual SQL only).
    op.execute(
        "ALTER TABLE session ALTER COLUMN status SET DEFAULT 'COMPLETE'"
    )
