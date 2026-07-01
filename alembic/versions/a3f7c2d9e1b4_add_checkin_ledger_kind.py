"""add CHECKIN to credit_ledger_kind

Daily check-in grants a fixed credit reward through a ``CHECKIN``
``credit_ledger`` row (see ``app/services/credits`` and the paired
``check_in`` table). Adds the ``CHECKIN`` value to the existing
``credit_ledger_kind`` enum.

``ALTER TYPE ... ADD VALUE`` is used (not the recreate-type swap) on
purpose: it's an O(1) catalog change with no rewrite/lock on the
``credit_ledger`` table, which matters for an append-only ledger that
grows unbounded. Kept in its own migration (mirroring the REFERRAL
add) so the ``create_check_in`` migration that follows runs in a
separate transaction and may freely use the now-committed label.

Downgrade is a no-op: Postgres has no ``DROP VALUE``, and removing it
via a type swap would rewrite the ledger and fail if any ``CHECKIN``
row exists. The unused extra label is harmless if left in place.

Revision ID: a3f7c2d9e1b4
Revises: f3a9c2e6b8d1
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a3f7c2d9e1b4"
down_revision: Union[str, Sequence[str], None] = "f3a9c2e6b8d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE credit_ledger_kind ADD VALUE IF NOT EXISTS 'CHECKIN'")


def downgrade() -> None:
    # No-op: an enum value can't be cleanly dropped (see module docstring).
    pass
