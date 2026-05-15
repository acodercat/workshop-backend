"""credit_ledger + credit_reservation + user.credit_balance

Storage layer for the PAYG credit system. See
``app/services/credits/`` for the surrounding logic and
``docs/credits-future.md`` for the deferred V2/V3 items
(sandbox-seconds, janitor, payment-driven top-ups).

Three new Postgres enums (auto-created by ``sa.Enum`` on first use
inside ``op.create_table``, matching the project convention used by
the ``user_feedback`` migration):

- ``credit_ledger_kind`` — TOPUP / RESERVE / SETTLE / REFUND / ADJUST
- ``credit_reservation_kind`` — TURN / SANDBOX (SANDBOX unused in V1)
- ``credit_reservation_state`` — OPEN / SETTLED / CANCELLED

Two new tables:

- ``credit_ledger``       — append-only signed-amount journal.
- ``credit_reservation``  — open-then-terminal lifecycle holds.

One new column on ``"user"``:

- ``credit_balance bigint NOT NULL DEFAULT 0`` — cached materialization
  of ``SUM(credit_ledger.amount)``; the ledger is the source of truth.

Partial indexes on the two ``state='OPEN'`` access paths keep the
lazy-sweep cost flat as the historical reservation table grows.

Revision ID: 9e3a8b714f02
Revises: 712ad01e4590
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9e3a8b714f02"
down_revision: Union[str, Sequence[str], None] = "712ad01e4590"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEDGER_KIND_NAME = "credit_ledger_kind"
_LEDGER_KIND_VALUES = ("TOPUP", "RESERVE", "SETTLE", "REFUND", "ADJUST")

_RESERVATION_KIND_NAME = "credit_reservation_kind"
_RESERVATION_KIND_VALUES = ("TURN", "SANDBOX")

_RESERVATION_STATE_NAME = "credit_reservation_state"
_RESERVATION_STATE_VALUES = ("OPEN", "SETTLED", "CANCELLED")


def upgrade() -> None:
    # 1) credit_ledger — append-only, indexed for per-user history.
    #    ``sa.Enum(...)`` auto-creates the PG type on first use; we
    #    don't pre-create it because the established project pattern
    #    (see ``712ad01e4590_add_user_feedback_table``) lets the
    #    enum-bearing ``create_table`` do it.
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*_LEDGER_KIND_VALUES, name=_LEDGER_KIND_NAME),
            nullable=False,
        ),
        sa.Column(
            "reference",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # UNIQUE rather than a plain index because the constraint is
        # the actual feature (idempotency); the resulting btree index
        # is also what lookups by key use.
        sa.UniqueConstraint(
            "idempotency_key", name="uq_credit_ledger_idempotency_key"
        ),
    )
    op.create_index(
        "ix_credit_ledger_user_created",
        "credit_ledger",
        ["user_id", sa.text("created_at DESC")],
    )

    # 2) credit_reservation — open-state lifecycle holds.
    op.create_table(
        "credit_reservation",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(*_RESERVATION_KIND_VALUES, name=_RESERVATION_KIND_NAME),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Uuid,
            sa.ForeignKey("session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("turn_id", sa.BigInteger, nullable=True),
        sa.Column("sandbox_id", sa.BigInteger, nullable=True),
        sa.Column("reserved", sa.BigInteger, nullable=False),
        sa.Column("rate_snapshot", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_RESERVATION_STATE_VALUES, name=_RESERVATION_STATE_NAME),
            nullable=False,
            server_default=sa.text(f"'OPEN'::{_RESERVATION_STATE_NAME}"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # Partial indexes — the two hot read paths only care about OPEN
    # rows; SETTLED / CANCELLED rows are historical and don't need to
    # be indexed.
    op.execute(
        "CREATE INDEX ix_credit_reservation_open_user "
        "ON credit_reservation (user_id) "
        "WHERE state = 'OPEN'"
    )
    op.execute(
        "CREATE INDEX ix_credit_reservation_open_session "
        "ON credit_reservation (session_id) "
        "WHERE state = 'OPEN'"
    )

    # 3) user.credit_balance — cached materialization. Existing rows
    #    default to 0 (no grandfathered credits); seeded admin accounts
    #    can be topped up via ``scripts/topup_credits.py``.
    op.add_column(
        "user",
        sa.Column(
            "credit_balance",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "credit_balance")
    # Partial indexes drop with the table.
    op.drop_table("credit_reservation")
    op.drop_index("ix_credit_ledger_user_created", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    # ``sa.Enum(name=...).drop(...)`` mirrors the user_feedback migration
    # — alembic doesn't auto-drop types on table drop, so we do it
    # explicitly. ``checkfirst=True`` makes the downgrade safe to re-run.
    sa.Enum(name=_RESERVATION_STATE_NAME).drop(op.get_bind(), checkfirst=True)
    sa.Enum(name=_RESERVATION_KIND_NAME).drop(op.get_bind(), checkfirst=True)
    sa.Enum(name=_LEDGER_KIND_NAME).drop(op.get_bind(), checkfirst=True)
