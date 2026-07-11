"""Immutable double-entry referral-commission ledger.

One row = one signed **cash** movement on one user's commission wallet.
Balance on :attr:`app.models.user.User.commission_balance_fen` is a
cached materialization of ``SUM(commission_ledger.amount_fen)`` per
user — the ledger is the **source of truth**; the column is a read
optimisation. Mirrors :mod:`app.models.credit_ledger` exactly, with one
deliberate difference: the unit is **RMB cents (整数分)**, not µ-credits,
because this wallet is real, withdrawable money (cash payable), a
distinct liability class from the prepaid service balance that
``credit_ledger`` tracks. The two ledgers are physically separate so
"how much cash do we owe in referral commission" is always answerable
without untangling it from service credits.

The ledger is append-only by contract (no ``UPDATE`` / ``DELETE`` —
corrections are a new signed ``ADJUST`` / ``CLAWBACK`` row), and all
writes funnel through :mod:`app.services.commission._internal` to keep
the invariant.

``available_at`` is the **settlement hold**: a ``COMMISSION`` row is
earned immediately but only *withdrawable* once ``now >= available_at``
(``created_at + commission_hold_days``), giving a window to claw back a
later-refunded recharge. Only ``COMMISSION`` rows carry it; every other
kind leaves it NULL (a debit / reversal is effective at once). The
withdrawable balance is ``total − SUM(unmatured COMMISSION)`` (see
:func:`app.services.commission.balance.get_balance`).

``idempotency_key`` is the single line of defense against duplicate
accrual on the payment provider's at-least-once retry: ``UNIQUE`` at
the DB layer means the second attempt fails on the constraint, the
transaction rolls back, the cached balance never moves.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CommissionLedgerKind(StrEnum):
    """Why a commission-ledger row exists.

    - ``COMMISSION``      — referral commission earned when an invitee
      recharges. Always positive ``amount_fen``; carries an
      ``idempotency_key`` (``commission:{out_trade_no}``) +
      ``reference.invitee_user_id`` / ``payment_order_id``. The only
      kind that sets ``available_at`` (the settlement hold).
    - ``WITHDRAW``        — funds moved out for a cash-out request.
      Always negative; paired with a ``withdrawal_request`` row by
      ``reference.withdrawal_id``.
    - ``WITHDRAW_REFUND`` — reversal of a ``WITHDRAW`` when its request
      is rejected. Always positive; ``reference.withdrawal_id``.
    - ``CONVERT``         — funds converted to spendable credits (the
      paired credit-in is a ``TOPUP`` row on ``credit_ledger``). Always
      negative; ``reference.credit_ledger_id``.
    - ``CLAWBACK``        — recovery of commission on a later-refunded
      recharge. Always negative; carries an ``idempotency_key`` +
      ``reference.reason``.
    - ``ADJUST``          — manual correction by an operator. Signed;
      carries an ``idempotency_key`` + ``reference.reason``.

    Member values match the Postgres ``commission_ledger_kind`` enum and
    are UPPERCASE, matching the ``credit_ledger_kind`` / ``session_status``
    convention.
    """

    COMMISSION = "COMMISSION"
    WITHDRAW = "WITHDRAW"
    WITHDRAW_REFUND = "WITHDRAW_REFUND"
    CONVERT = "CONVERT"
    CLAWBACK = "CLAWBACK"
    ADJUST = "ADJUST"


class CommissionLedger(Base):
    __tablename__ = "commission_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        # ``RESTRICT`` rather than ``CASCADE`` — deleting a user with a
        # non-zero commission ledger should fail loudly so accidental
        # deletions can't silently drop cash-liability history (mirrors
        # ``credit_ledger.user_id``).
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Signed RMB cents (整数分). Integer money — no float, mirroring
    # ``payment_order.amount_fen``.
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[CommissionLedgerKind] = mapped_column(
        SAEnum(
            CommissionLedgerKind,
            name="commission_ledger_kind",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # Settlement hold: set to ``created_at + commission_hold_days`` on
    # ``COMMISSION`` rows; NULL on every other kind (effective at once).
    # A ``COMMISSION`` row counts toward the withdrawable balance only
    # once ``now >= available_at``.
    available_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    reference: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict,
    )
    # NULL on internal-only kinds; required (UNIQUE) on ``COMMISSION`` /
    # ``CLAWBACK`` / ``ADJUST`` so accrual + operator corrections retry
    # safely under the provider's at-least-once delivery.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        # Backs ``GET /commissions/transactions`` history pagination.
        # Postgres doesn't auto-index FK columns; without this the
        # per-user list would seq-scan the whole ledger.
        Index(
            "ix_commission_ledger_user_created",
            "user_id",
            text("created_at DESC"),
        ),
        # The withdrawable-balance read sums a user's still-frozen
        # ``COMMISSION`` rows (``available_at > now``). Partial so it
        # stays tiny (only commission accruals) and matches that filter.
        Index(
            "ix_commission_ledger_user_unmatured",
            "user_id",
            "available_at",
            postgresql_where=text("kind = 'COMMISSION'"),
        ),
        # DB backstop for the per-kind sign conventions documented on
        # :class:`CommissionLedgerKind` — a stray manual ``psql`` UPDATE
        # or buggy operator script can't silently corrupt a cash-payable
        # balance. Enforced in Python by the ``_internal`` funnel; this
        # mirrors ``ck_credit_ledger_amount_sign`` (migration
        # ``b2d6f1a8c4e3``).
        CheckConstraint(
            "(kind = 'COMMISSION'      AND amount_fen > 0)  OR "
            "(kind = 'WITHDRAW'        AND amount_fen < 0)  OR "
            "(kind = 'WITHDRAW_REFUND' AND amount_fen > 0)  OR "
            "(kind = 'CONVERT'         AND amount_fen < 0)  OR "
            "(kind = 'CLAWBACK'        AND amount_fen < 0)  OR "
            "(kind = 'ADJUST'          AND amount_fen <> 0)",
            name="ck_commission_ledger_amount_sign",
        ),
        # The settlement hold rides on ``COMMISSION`` rows and only on
        # them (see the module docstring); same migration.
        CheckConstraint(
            "(kind = 'COMMISSION') = (available_at IS NOT NULL)",
            name="ck_commission_ledger_kind_available_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CommissionLedger(id={self.id}, user_id={self.user_id}, "
            f"kind='{self.kind.value}', amount_fen={self.amount_fen})>"
        )
