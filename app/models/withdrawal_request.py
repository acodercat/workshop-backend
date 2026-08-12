"""Cash-out requests against the referral-commission wallet.

A ``withdrawal_request`` is the durable record of a user asking to
withdraw their commission balance to their 支付宝 account (Alipay-only
for now). v1 settlement is **manual**: the operator reviews PENDING
requests and pays them out in a batch (``scripts/process_withdrawals.py``),
flipping each to PAID with a payout reference or REJECTED with a reason.

The money leaves the wallet at **request** time, not payout time — a
``WITHDRAW`` ledger row (negative ``amount_fen``) is posted alongside
the PENDING request so the funds can't be double-spent across two open
requests (mirrors how a ``credit_reservation`` debits the balance up
front). ``ledger_id`` back-links that row. On rejection a
``WITHDRAW_REFUND`` row returns the funds; on payout no ledger change is
needed (the debit already happened). This mirrors the
``payment_order`` → ``credit_ledger`` back-link in the buy-side flow.

``id`` is a UUID PK so it's opaque in ``GET /commissions/withdrawals``
and can't be enumerated across users — same rationale as
``payment_order.id``.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class WithdrawalStatus(StrEnum):
    """Lifecycle status of a withdrawal request.

    - ``PENDING``  — submitted; funds already debited via a ``WITHDRAW``
      ledger row, awaiting operator review. Default for a fresh row; the
      only non-terminal state.
    - ``PAID``     — operator paid it out; ``payout_ref`` records the
      transfer reference. Terminal.
    - ``REJECTED`` — operator declined; a ``WITHDRAW_REFUND`` row
      returned the funds and ``reject_reason`` records why. Terminal.

    Member values match the Postgres ``withdrawal_status`` enum;
    UPPERCASE per project convention.
    """

    PENDING = "PENDING"
    PAID = "PAID"
    REJECTED = "REJECTED"


class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_request"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # ``RESTRICT`` rather than ``CASCADE`` — a withdrawal is cash-payout
    # history; deleting a user with payout rows should fail loudly rather
    # than silently drop the audit trail (mirrors ``payment_order.user_id``).
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Amount requested, in RMB cents (整数分). Integer money — no float.
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(
        SAEnum(
            WithdrawalStatus,
            name="withdrawal_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    # The user's 支付宝 account + the account holder's name, for the
    # operator's manual transfer. Payouts are Alipay-only.
    payout_account: Mapped[str] = mapped_column(String(128), nullable=False)
    payout_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Back-link to the ``WITHDRAW`` ledger row that debited the funds,
    # set atomically with the PENDING insert. NOT NULL (migration
    # ``e1c5a9d7b3f4``) makes that atomicity a DB contract: a request
    # whose funds were never held — the double-spend the up-front debit
    # exists to prevent — can't be inserted. ``RESTRICT`` because the
    # ledger is append-only (a row is never deleted, so this never dangles).
    ledger_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("commission_ledger.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The operator's transfer reference, filled when paid. NULL until PAID.
    payout_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Why a request was rejected, filled when rejected. NULL otherwise.
    reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    # When the request reached a terminal state (PAID / REJECTED). NULL
    # while PENDING.
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # Backs ``GET /commissions/withdrawals`` history listing (newest
        # first). Postgres doesn't auto-index FK columns; without this a
        # per-user list would seq-scan the whole table.
        Index(
            "ix_withdrawal_request_user_created",
            "user_id",
            text("created_at DESC"),
        ),
        # The operator's batch review scans only actionable PENDING
        # requests. Partial so it stays tiny forever as paid/rejected
        # history grows — mirrors ``payment_order``'s PENDING partial index.
        Index(
            "ix_withdrawal_request_pending",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WithdrawalRequest(id={self.id}, user_id={self.user_id}, "
            f"amount_fen={self.amount_fen}, status={self.status.value!r})>"
        )
