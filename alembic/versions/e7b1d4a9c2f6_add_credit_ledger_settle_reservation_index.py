"""add partial expression index for the transactions-view settle probe

Backs the correlated subquery in
``app.services.credits.transactions.py`` that, per SETTLED reservation
on a transactions page, looks up the actual-cost ledger row:
``SELECT amount FROM credit_ledger
   WHERE (reference->>'reservation_id')::bigint = cr.id
     AND kind = 'SETTLE' AND amount < 0``.

Without an index that probe seq-scans the whole ledger once per row on
the page — fine at today's scale, O(page x ledger) as the ledger grows,
on an endpoint that also runs the lazy settle sweep.

A *partial expression* index keyed on
``(reference->>'reservation_id')::bigint`` and confined to the
``kind='SETTLE' AND amount<0`` rows makes the probe a point lookup while
staying tiny (only the ``-actual`` settle rows, one per reservation).
The expression + partial predicate mirror the subquery exactly so the
planner uses it.

Built ``CONCURRENTLY`` so it doesn't lock writes on a populated
``credit_ledger`` — that requires running outside Alembic's wrapping
transaction, hence the ``autocommit_block``.

Revision ID: e7b1d4a9c2f6
Revises: f3b8d1e6c9a2
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b1d4a9c2f6"
down_revision: Union[str, Sequence[str], None] = "f3b8d1e6c9a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_credit_ledger_settle_reservation"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "credit_ledger",
            [sa.text("((reference ->> 'reservation_id')::bigint)")],
            postgresql_concurrently=True,
            postgresql_where=sa.text("kind = 'SETTLE' AND amount < 0"),
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="credit_ledger",
            postgresql_concurrently=True,
            if_exists=True,
        )
