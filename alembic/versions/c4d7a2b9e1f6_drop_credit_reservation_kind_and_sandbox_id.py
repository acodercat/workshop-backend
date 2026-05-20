"""credit_reservation: drop kind + sandbox_id (V2-reserved fields, never used)

``CreditReservationKind`` was created with two values, ``TURN`` and
``SANDBOX``; only ``TURN`` was ever written. ``sandbox_id`` was a
nullable companion column for the ``SANDBOX`` variant that never
materialized — the three-factor billing introduced ``per_second_credits``
on a single ``TURN`` reservation instead, so the V2 hook is no longer
needed.

- ``DROP COLUMN`` on a NOT NULL enum column is an instant catalog-only
  change in Postgres (no table rewrite for column drops).
- The ``credit_reservation_kind`` enum type is dropped last, after the
  column that referenced it; nothing else in the schema uses it.

Revision ID: c4d7a2b9e1f6
Revises: a6f4d2c9e1b8
Create Date: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d7a2b9e1f6"
down_revision: Union[str, Sequence[str], None] = "a6f4d2c9e1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KIND_ENUM_NAME = "credit_reservation_kind"
_KIND_VALUES = ("TURN", "SANDBOX")


def upgrade() -> None:
    op.drop_column("credit_reservation", "sandbox_id")
    op.drop_column("credit_reservation", "kind")
    sa.Enum(name=_KIND_ENUM_NAME).drop(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    # Recreate the enum, then the columns, in the original shape from
    # 9e3a8b714f02. ``kind`` was NOT NULL with no default; on downgrade
    # we have no truthful historical value to backfill, so set every
    # row to ``TURN`` (the only value V1 ever wrote anyway).
    sa.Enum(*_KIND_VALUES, name=_KIND_ENUM_NAME).create(op.get_bind())
    op.add_column(
        "credit_reservation",
        sa.Column(
            "kind",
            sa.Enum(*_KIND_VALUES, name=_KIND_ENUM_NAME, create_type=False),
            nullable=True,
        ),
    )
    op.execute("UPDATE credit_reservation SET kind = 'TURN'")
    op.alter_column("credit_reservation", "kind", nullable=False)
    op.add_column(
        "credit_reservation",
        sa.Column("sandbox_id", sa.BigInteger, nullable=True),
    )
