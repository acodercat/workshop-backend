"""Immutable double-entry credit ledger.

One row = one signed credit movement on one user's account. Balance
on :attr:`app.models.user.User.credit_balance` is a cached
materialization of ``SUM(credit_ledger.amount)`` per user — the
ledger is the **source of truth**; the column is a read optimisation.

The ledger is append-only by contract:

- No ``UPDATE`` — corrections are a new ``ADJUST`` row with a signed
  ``amount`` that undoes the bad row.
- No ``DELETE`` — ditto.

That contract is the entire reason the system can answer "where did
my credits go" after an incident. All writes funnel through
:mod:`app.services.credits._internal` to keep the invariant.

``kind`` is a Postgres native enum mirroring the project convention
(see :class:`SessionStatus`). ``reference`` is JSONB — different
``kind``s correlate to different business entities, and a discriminated
column-per-kind would be a long sparse table.

``idempotency_key`` is the single line of defense against duplicate
top-ups / adjustments on retry: ``UNIQUE`` enforced at the DB layer
means the second attempt fails on the constraint, the transaction
rolls back, the cached balance never moves. System-emitted rows
(``RESERVE`` / ``SETTLE`` / ``REFUND``) leave it NULL — their
idempotency is enforced by the reservation state machine.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
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


class CreditLedgerKind(StrEnum):
    """Why a ledger row exists.

    - ``TOPUP``    — external credit-in (admin script today, payment
      provider in V3). Always positive ``amount``. Carries an
      ``idempotency_key``.
    - ``RESERVE``  — funds locked at the start of a turn. Always
      negative; paired with later ``SETTLE`` / ``REFUND`` row(s) by
      ``reference.reservation_id``.
    - ``SETTLE``   — closing a reservation with actual usage. Two rows
      per settle: one ``+reserved`` reversing the reservation, one
      ``-actual`` debiting the real cost.
    - ``REFUND``   — closing a reservation with zero actual cost
      (worker crash, cancellation, etc.). One ``+reserved`` row.
    - ``ADJUST``   — manual correction by an operator. Signed; carries
      an ``idempotency_key`` + ``reference.reason``.
    - ``REFERRAL`` — reward granted to a referrer when someone registers
      via their referral code. Always positive; carries an
      ``idempotency_key`` (``referral-{invitee_id}``) +
      ``reference.invitee_user_id``.

    Member values match the Postgres ``credit_ledger_kind`` enum and
    are intentionally UPPERCASE matching the project convention used
    by :class:`SessionStatus`, :class:`UserStatus`, etc.
    """

    TOPUP = "TOPUP"
    RESERVE = "RESERVE"
    SETTLE = "SETTLE"
    REFUND = "REFUND"
    ADJUST = "ADJUST"
    REFERRAL = "REFERRAL"


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        # ``RESTRICT`` rather than ``CASCADE`` — deleting a user with a
        # non-zero ledger should fail loudly so accidental deletions
        # can't silently drop audit history. Real user deletion is a
        # two-step: zero the balance via ``ADJUST``, then delete.
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[CreditLedgerKind] = mapped_column(
        SAEnum(
            CreditLedgerKind,
            name="credit_ledger_kind",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    reference: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict,
    )
    # NULL on system-emitted rows (RESERVE / SETTLE / REFUND) where the
    # reservation state machine prevents duplicates anyway. Required
    # on TOPUP / ADJUST so admin scripts / future payment webhooks can
    # retry safely.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        # Backs ``GET /credits`` history pagination. Postgres doesn't
        # auto-index FK columns; without this the per-user list would
        # seq-scan the whole ledger.
        Index(
            "ix_credit_ledger_user_created",
            "user_id",
            text("created_at DESC"),
        ),
        # Point-lookup for the transactions view's per-reservation probe
        # (``services/credits/transactions.py``): the negative SETTLE row
        # for a given reservation. Without it that correlated subquery
        # seq-scans the whole ledger once per SETTLED reservation on the
        # page. Partial + expression so it stays tiny (only the ``-actual``
        # settle rows) and matches the subquery's
        # ``(reference->>'reservation_id')::bigint`` /
        # ``kind='SETTLE' AND amount<0`` filters exactly.
        Index(
            "ix_credit_ledger_settle_reservation",
            text("((reference ->> 'reservation_id')::bigint)"),
            postgresql_where=text("kind = 'SETTLE' AND amount < 0"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CreditLedger(id={self.id}, user_id={self.user_id}, "
            f"kind='{self.kind.value}', amount={self.amount})>"
        )
