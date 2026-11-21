"""Open credit holds that the settle path closes out.

One row = one "I might spend up to X µ-credits on this turn"
reservation. The row lives in three states (``OPEN`` → ``SETTLED`` |
``CANCELLED``); the lifecycle is the entire reason this table exists
in addition to :class:`CreditLedger`:

- The matching ``RESERVE`` ledger row only records the *amount* held,
  not the *context* (which session / turn / rate snapshot). The
  reservation row carries that context so the settle path can compute
  actual cost without re-reading the rate-card.
- The partial index on ``state='OPEN'`` lets the lazy sweep find
  abandoned holds in one indexed lookup per user.
- ``rate_snapshot`` (JSONB) freezes the price at reserve time. A
  config change between reserve and settle does not re-price the turn
  — important for any pricing change to remain customer-trust-safe.
"""

import uuid
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
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CreditReservationState(StrEnum):
    """Closed three-state FSM: ``OPEN`` → ``SETTLED`` or ``CANCELLED``.

    Once terminal, never re-opened. A correction goes through a fresh
    ``ADJUST`` ledger row, not by mutating reservation state.
    """

    OPEN = "OPEN"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


class CreditReservation(Base):
    __tablename__ = "credit_reservation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # ``SET NULL`` rather than ``CASCADE`` — if a session is deleted,
    # keep the reservation history for audit; it just loses the
    # session-back link.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="SET NULL"),
        nullable=True,
    )
    # NOT NULL: every reservation is created for a specific turn by
    # ``reserve_for_turn``. No FK on the column — ``session_turn``
    # cascades from its parent ``session``, and a broken back-link is
    # preferable to a deleted reservation row (we keep the row for
    # audit even when the turn has been purged).
    turn_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[CreditReservationState] = mapped_column(
        SAEnum(
            CreditReservationState,
            name="credit_reservation_state",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=CreditReservationState.OPEN,
        server_default=text("'OPEN'::credit_reservation_state"),
    )
    # Used by the sweeper to find abandoned reservations from worker
    # crashes (``expires_at < now() AND session non-terminal`` →
    # refund). ``timezone=True`` keeps the tz-aware datetime
    # written by reserve.py round-trip correctly (the bare ``TIMESTAMP``
    # type silently strips tz on write and yields naive on read, which
    # breaks any Python-side comparison against ``datetime.now(utc)``).
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # Partial index — typical user has 0 or 1 OPEN reservation, so
        # this stays small forever even as the historical reservation
        # table grows. Lazy sweep reads ``WHERE user_id = ? AND state =
        # 'OPEN'`` once per user request.
        Index(
            "ix_credit_reservation_open_user",
            "user_id",
            postgresql_where=text("state = 'OPEN'"),
        ),
        # Backs ``analytics.session_spend``: "what did these sessions
        # cost" filters reservations by session, then joins the ledger
        # through ``ix_credit_ledger_reservation_id``. The partial index
        # below cannot serve it — that one covers only OPEN rows, and a
        # settled session is exactly the case being asked about. Plain,
        # not partial: every reservation carrying a session is in scope.
        Index("ix_credit_reservation_session_id", "session_id"),
        # DB backstop for the settle path: one session may have at most
        # one OPEN reservation. Historical settled rows remain
        # unconstrained, and NULL session reservations are not part of
        # this session-scoped invariant.
        Index(
            "uq_credit_reservation_open_session",
            "session_id",
            unique=True,
            postgresql_where=text("state = 'OPEN' AND session_id IS NOT NULL"),
        ),
        # ``settled_at`` coherence with ``state`` — both terminal
        # transitions stamp ``settled_at = now()``. Text matches the live
        # constraint from ``f5d2c8a1e9b4``; declared here so a
        # ``create_all``-built DB carries the same guardrail and the
        # model documents its own contract.
        CheckConstraint(
            "(state = 'OPEN' AND settled_at IS NULL) OR "
            "(state IN ('SETTLED', 'CANCELLED') AND settled_at IS NOT NULL)",
            name="ck_credit_reservation_state_settled_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CreditReservation(id={self.id}, user_id={self.user_id}, "
            f"state='{self.state.value}', reserved={self.reserved})>"
        )
