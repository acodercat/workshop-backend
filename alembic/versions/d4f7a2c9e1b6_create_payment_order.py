"""create payment_order

Buy-side payment orders for RMB top-ups (汇付斗拱 / Huifu). One row per
recharge attempt; bridges an external payment to a TOPUP ledger row via
``credits.apply_topup`` (see ``app/models/payment_order.py``).

Schema decisions:

- ``id`` is a UUID PK (opaque in ``GET /payments/orders/{id}``), distinct
  from ``out_trade_no`` (the provider-facing business key, UNIQUE).
- ``provider`` / ``status`` are typed Postgres enums
  (``payment_provider`` / ``payment_order_status``) with UPPERCASE labels
  matching the Python ``StrEnum`` members.
- ``amount_fen`` is integer RMB cents; ``credit_micro`` is the frozen
  µ-credit grant — both integer money, no float.
- ``ledger_id`` back-links the TOPUP row (``RESTRICT``; the ledger is
  append-only so it never dangles).
- Partial index ``ix_payment_order_pending`` covers the reconciler's
  hot scan over PENDING orders by expiry; terminal rows are excluded so
  it stays small.

Revision ID: d4f7a2c9e1b6
Revises: c8d4a1f7e3b9
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4f7a2c9e1b6"
down_revision: Union[str, Sequence[str], None] = "c8d4a1f7e3b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROVIDER_NAME = "payment_provider"
_PROVIDER_VALUES = ("HUIFU",)
_STATUS_NAME = "payment_order_status"
_STATUS_VALUES = ("PENDING", "PAID", "EXPIRED", "FAILED")


def upgrade() -> None:
    # Raw SQL CREATE TYPE — sa.Enum's metadata-bound auto-create has
    # surprising interactions with create_table when create_type=False
    # is set on the column-side enum, so emit both explicitly here.
    op.execute(
        f"CREATE TYPE {_PROVIDER_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _PROVIDER_VALUES)})"
    )
    op.execute(
        f"CREATE TYPE {_STATUS_NAME} AS ENUM "
        f"({', '.join(repr(v) for v in _STATUS_VALUES)})"
    )

    op.create_table(
        "payment_order",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            postgresql.ENUM(*_PROVIDER_VALUES, name=_PROVIDER_NAME, create_type=False),
            nullable=False,
        ),
        sa.Column("out_trade_no", sa.String(length=64), nullable=False),
        sa.Column("provider_txn_id", sa.String(length=64), nullable=True),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("credit_micro", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_STATUS_VALUES, name=_STATUS_NAME, create_type=False),
            nullable=False,
            server_default=sa.text(f"'PENDING'::{_STATUS_NAME}"),
        ),
        sa.Column(
            "ledger_id",
            sa.BigInteger(),
            sa.ForeignKey("credit_ledger.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "reference",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
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
    )

    op.create_unique_constraint(
        "uq_payment_order_out_trade_no", "payment_order", ["out_trade_no"],
    )
    op.create_index(
        "ix_payment_order_user_created",
        "payment_order",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_payment_order_pending",
        "payment_order",
        ["expires_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_payment_order_pending", table_name="payment_order")
    op.drop_index("ix_payment_order_user_created", table_name="payment_order")
    op.drop_constraint(
        "uq_payment_order_out_trade_no", "payment_order", type_="unique",
    )
    op.drop_table("payment_order")
    op.execute(f"DROP TYPE IF EXISTS {_STATUS_NAME}")
    op.execute(f"DROP TYPE IF EXISTS {_PROVIDER_NAME}")
