"""create check_in table

Daily check-in: once per business-timezone calendar day a user claims a
fixed credit reward. One append-only ``check_in`` row per (user, day)
records the event + its consecutive-day ``streak_day``, back-linking the
paired ``CHECKIN`` ``credit_ledger`` grant via ``ledger_id``. The reward
amount lives in the ledger, not here (mirrors ``invite_redemption``).

``UNIQUE(user_id, check_in_date)`` is the DB-level "one per day" guard;
the composite index it creates also serves the streak-lookup (latest row
per user, backward scan) and per-user history reads, so no separate
index is created. Both FKs are ``RESTRICT`` — this row is provenance.

Schema decisions mirror the existing audit tables (``invite_redemption``
/ ``credit_ledger``): ``RESTRICT`` FKs, append-only with only
``created_at``, BigInteger PK for an accretive (per-user-per-day) table.

Revision ID: b8d1e4a7c3f6
Revises: a3f7c2d9e1b4
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d1e4a7c3f6"
down_revision: Union[str, Sequence[str], None] = "a3f7c2d9e1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "check_in",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("check_in_date", sa.Date(), nullable=False),
        sa.Column("streak_day", sa.Integer(), nullable=False),
        sa.Column(
            "ledger_id",
            sa.BigInteger(),
            sa.ForeignKey("credit_ledger.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_check_in_user_date",
        "check_in",
        ["user_id", "check_in_date"],
    )


def downgrade() -> None:
    op.drop_table("check_in")
