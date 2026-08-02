"""promote credit_ledger.reservation_id from JSONB to a typed FK column

``reservation_id`` was a load-bearing relational key living inside the
``reference`` JSONB (on SETTLE / REFUND rows). Being a JSONB key it had no
referential integrity, no native index, and no type safety — the transactions
view had to ``(reference->>'reservation_id')::bigint`` and lean on an
expression index. This promotes it to a first-class nullable FK column:

- **Backfill both directions.** SETTLE / REFUND rows carried it in
  ``reference`` → copy it out, then strip the key so the column is the single
  source of truth (no duplicated pointer left behind). RESERVE rows never
  carried it in JSONB → recover it from the reservation via the 1:1
  ``turn_id`` (matching the new code path that now stamps it on RESERVE).
- **Swap the index.** The JSONB-expression partial index is replaced by a
  plain column index (partial on ``reservation_id IS NOT NULL``), covering the
  whole trail — RESERVE / SETTLE / REFUND — not just the settle row.

Small table (~tens of thousands of rows): the column add is metadata-only, the
backfill UPDATEs and index build take only a brief lock.

Revision ID: e7c4a9b2f1d8
Revises: c1e5b8a3d7f2
Create Date: 2026-07-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e7c4a9b2f1d8"
down_revision: Union[str, Sequence[str], None] = "c1e5b8a3d7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New typed column — nullable, since only turn-related rows link to a
    #    hold (TOPUP / ADJUST / REFERRAL / CHECKIN stay NULL).
    op.add_column(
        "credit_ledger",
        sa.Column("reservation_id", sa.BigInteger(), nullable=True),
    )

    # 2. Backfill SETTLE / REFUND from their old JSONB home AND strip the key
    #    in the same statement, so no row is left with the pointer in two
    #    places.
    op.execute(
        """
        UPDATE credit_ledger
           SET reservation_id = (reference ->> 'reservation_id')::bigint,
               reference      = reference - 'reservation_id'
         WHERE reference ? 'reservation_id'
        """
    )
    # 3. Backfill RESERVE from the reservation itself via the 1:1 turn_id
    #    (RESERVE never stored reservation_id in JSONB). Leaves the rest of the
    #    RESERVE reference (session_id / turn_id / model_id) untouched.
    op.execute(
        """
        UPDATE credit_ledger cl
           SET reservation_id = cr.id
          FROM credit_reservation cr
         WHERE cl.kind = 'RESERVE'
           AND cl.reservation_id IS NULL
           AND (cl.reference ->> 'turn_id')::bigint = cr.turn_id
        """
    )

    # 4. FK last, so it validates the freshly-backfilled values in one pass —
    #    an orphan pointer would fail here, loud. RESTRICT mirrors the ledger's
    #    user_id FK: a reservation with ledger rows can't be deleted.
    op.create_foreign_key(
        "credit_ledger_reservation_id_fkey",
        "credit_ledger",
        "credit_reservation",
        ["reservation_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 5. Replace the JSONB-expression index with a plain column index.
    op.drop_index("ix_credit_ledger_settle_reservation", table_name="credit_ledger")
    op.create_index(
        "ix_credit_ledger_reservation_id",
        "credit_ledger",
        ["reservation_id"],
        postgresql_where=sa.text("reservation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_reservation_id", table_name="credit_ledger")

    # Re-materialize the pointer back into ``reference`` for the rows that
    # originally carried it there (SETTLE / REFUND). RESERVE never had it in
    # JSONB, so it is intentionally not restored there.
    op.execute(
        """
        UPDATE credit_ledger
           SET reference = jsonb_set(
                   reference, '{reservation_id}', to_jsonb(reservation_id)
               )
         WHERE reservation_id IS NOT NULL
           AND kind IN ('SETTLE', 'REFUND')
        """
    )
    op.create_index(
        "ix_credit_ledger_settle_reservation",
        "credit_ledger",
        [sa.text("((reference ->> 'reservation_id')::bigint)")],
        postgresql_where=sa.text("kind = 'SETTLE' AND amount < 0"),
    )
    op.drop_constraint(
        "credit_ledger_reservation_id_fkey", "credit_ledger", type_="foreignkey",
    )
    op.drop_column("credit_ledger", "reservation_id")
