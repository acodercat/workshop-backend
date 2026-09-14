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
(see :class:`SessionStatus`). ``reference`` is JSONB for the *variable*
per-kind audit payload (cause / reason / actual token breakdown / …) —
a discriminated column-per-kind would be a long sparse table. The one
relational key that used to live in it, ``reservation_id``, is now a
typed FK column (see below): it is an identity, not variable metadata,
so it earns integrity + a native index that a JSONB key can't give.

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
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
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
      the ``reservation_id`` column.
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
    - ``CHECKIN``  — daily check-in reward, one per user per business-day
      (paired with a ``check_in`` row). Always positive; carries an
      ``idempotency_key`` (``checkin:{user_id}:{date}``) +
      ``reference.check_in_date`` / ``reference.streak_day``.

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
    CHECKIN = "CHECKIN"


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
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    # The turn hold this row belongs to (``credit_reservation.id``). A typed,
    # FK-backed column rather than a ``reference`` JSONB key because it is a
    # relational identity, not variable audit metadata — so it carries
    # referential integrity + a native index + type safety the JSONB key
    # couldn't. NULL on rows unrelated to a hold (TOPUP / ADJUST / REFERRAL /
    # CHECKIN); set on every row of a turn's credit trail — RESERVE, SETTLE,
    # REFUND — so they all join back through one column.
    reservation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("credit_reservation.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # NULL on system-emitted rows (RESERVE / SETTLE / REFUND) where the
    # reservation state machine prevents duplicates anyway. Required
    # on TOPUP / ADJUST so admin scripts / future payment webhooks can
    # retry safely.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
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
        # Backs the transactions view's per-reservation probe
        # (``services/credits/transactions.py``) and any "ledger trail for
        # this hold" lookup. Partial on ``reservation_id IS NOT NULL`` so it
        # stays tiny (only turn-related rows carry one) while covering the
        # whole trail — RESERVE / SETTLE / REFUND — not just the settle row.
        Index(
            "ix_credit_ledger_reservation_id",
            "reservation_id",
            postgresql_where=text("reservation_id IS NOT NULL"),
        ),
        # DB backstop for the per-kind sign conventions (enforced in
        # Python by the ``_internal`` funnel). Text matches the live
        # constraint exactly — created in ``f5d2c8a1e9b4``, extended for
        # REFERRAL in ``d8b3f6a1c402`` and CHECKIN in ``c1e5b8a3d7f2``.
        # SETTLE is deliberately unconstrained: two paired rows per
        # settle (``+reserved`` reversal, ``-actual`` debit), and actual
        # can be 0 for a zero-token turn. Declared here (not only in the
        # migrations) so a ``create_all``-built DB carries the same
        # guardrails and the model documents its own contract.
        CheckConstraint(
            "(kind = 'TOPUP'    AND amount > 0)  OR "
            "(kind = 'RESERVE'  AND amount < 0)  OR "
            "(kind = 'REFUND'   AND amount > 0)  OR "
            "(kind = 'ADJUST'   AND amount <> 0) OR "
            "(kind = 'REFERRAL' AND amount > 0)  OR "
            "(kind = 'CHECKIN'  AND amount > 0)  OR "
            "(kind = 'SETTLE')",
            name="ck_credit_ledger_amount_sign",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CreditLedger(id={self.id}, user_id={self.user_id}, "
            f"kind='{self.kind.value}', amount={self.amount})>"
        )
