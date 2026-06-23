"""add REFERRAL to credit_ledger_kind

Referral rewards credit the referrer when an invitee registers via
their personal code (see ``app/services/credits/referral.py``). Adds
the ``REFERRAL`` value to the existing ``credit_ledger_kind`` enum.

``ALTER TYPE ... ADD VALUE`` is used (not the recreate-type swap) on
purpose: it's an O(1) catalog change with no rewrite/lock on the
``credit_ledger`` table, which matters for an append-only ledger that
grows unbounded. PG 12+ allows it inside a transaction as long as the
value isn't used in the same transaction (it isn't here).

Downgrade is a no-op: Postgres has no ``DROP VALUE``, and removing it
via a type swap would rewrite the ledger and fail if any ``REFERRAL``
row exists. The unused extra label is harmless if left in place.

Revision ID: c7a2f4e9b108
Revises: b5e1c9d3a720
Create Date: 2026-06-23

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c7a2f4e9b108"
down_revision: Union[str, Sequence[str], None] = "b5e1c9d3a720"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE credit_ledger_kind ADD VALUE IF NOT EXISTS 'REFERRAL'")


def downgrade() -> None:
    # No-op: an enum value can't be cleanly dropped (see module docstring).
    pass
