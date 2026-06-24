"""create commission wallet

Referral-commission wallet: when an invitee recharges, the inviter earns
a withdrawable **cash** commission. Two tables + one user column:

- ``commission_ledger`` — append-only double-entry ledger in RMB cents
  (整数分), the cash-payable sibling of ``credit_ledger`` (see
  ``app/models/commission_ledger.py``). ``available_at`` is the
  settlement hold on ``COMMISSION`` rows.
- ``withdrawal_request`` — manual cash-out requests; the money is
  debited at request time via a ``WITHDRAW`` ledger row that
  ``ledger_id`` back-links.
- ``user.commission_balance_fen`` — cached ``SUM(commission_ledger
  .amount_fen)``, mirroring ``user.credit_balance``.

Schema decisions mirror the existing ledger / payment_order tables:
hand-written Postgres enums with UPPERCASE labels, ``RESTRICT`` FKs on
audit rows, integer money, and partial indexes for the hot read paths
(per-user history, still-frozen accruals, PENDING withdrawals).

Revision ID: f3a9c2e6b8d1
Revises: e5c1a9b7d2f4
Create Date: 2026-06-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f3a9c2e6b8d1"
down_revision: Union[str, Sequence[str], None] = "e5c1a9b7d2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEDGER_KIND_NAME = "commission_ledger_kind"
_LEDGER_KIND_VALUES = (
    "COMMISSION", "WITHDRAW", "WITHDRAW_REFUND", "CONVERT", "CLAWBACK", "ADJUST",
)
_WITHDRAWAL_STATUS_NAME = "withdrawal_status"
_WITHDRAWAL_STATUS_VALUES = ("PENDING", "PAID", "REJECTED")
_PAYOUT_CHANNEL_NAME = "withdrawal_payout_channel"
_PAYOUT_CHANNEL_VALUES = ("ALIPAY", "BANK")


def upgrade() -> None:
    # Raw SQL CREATE TYPE — sa.Enum's metadata-bound auto-create has
    # surprising interactions with create_table when create_type=False
    # is set on the column-side enum, so emit all three explicitly here.
    op.execute(
        f"CREATE TYPE {_LEDGER_KIND_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _LEDGER_KIND_VALUES)})"
    )
    op.execute(
        f"CREATE TYPE {_WITHDRAWAL_STATUS_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _WITHDRAWAL_STATUS_VALUES)})"
    )
    op.execute(
        f"CREATE TYPE {_PAYOUT_CHANNEL_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _PAYOUT_CHANNEL_VALUES)})"
    )

    op.create_table(
        "commission_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                *_LEDGER_KIND_VALUES, name=_LEDGER_KIND_NAME, create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("available_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "reference",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_commission_ledger_idempotency_key",
        "commission_ledger",
        ["idempotency_key"],
    )
    op.create_index(
        "ix_commission_ledger_user_created",
        "commission_ledger",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_commission_ledger_user_unmatured",
        "commission_ledger",
        ["user_id", "available_at"],
        postgresql_where=sa.text("kind = 'COMMISSION'"),
    )

    op.create_table(
        "withdrawal_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *_WITHDRAWAL_STATUS_VALUES,
                name=_WITHDRAWAL_STATUS_NAME,
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text(f"'PENDING'::{_WITHDRAWAL_STATUS_NAME}"),
        ),
        sa.Column(
            "payout_channel",
            postgresql.ENUM(
                *_PAYOUT_CHANNEL_VALUES,
                name=_PAYOUT_CHANNEL_NAME,
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("payout_account", sa.String(length=128), nullable=False),
        sa.Column("payout_name", sa.String(length=64), nullable=False),
        sa.Column(
            "ledger_id",
            sa.BigInteger(),
            sa.ForeignKey("commission_ledger.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("payout_ref", sa.String(length=128), nullable=True),
        sa.Column("reject_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_withdrawal_request_user_created",
        "withdrawal_request",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_withdrawal_request_pending",
        "withdrawal_request",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.add_column(
        "user",
        sa.Column(
            "commission_balance_fen",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "commission_balance_fen")

    op.drop_index("ix_withdrawal_request_pending", table_name="withdrawal_request")
    op.drop_index("ix_withdrawal_request_user_created", table_name="withdrawal_request")
    op.drop_table("withdrawal_request")

    op.drop_index("ix_commission_ledger_user_unmatured", table_name="commission_ledger")
    op.drop_index("ix_commission_ledger_user_created", table_name="commission_ledger")
    op.drop_constraint(
        "uq_commission_ledger_idempotency_key", "commission_ledger", type_="unique",
    )
    op.drop_table("commission_ledger")

    op.execute(f"DROP TYPE IF EXISTS {_PAYOUT_CHANNEL_NAME}")
    op.execute(f"DROP TYPE IF EXISTS {_WITHDRAWAL_STATUS_NAME}")
    op.execute(f"DROP TYPE IF EXISTS {_LEDGER_KIND_NAME}")
