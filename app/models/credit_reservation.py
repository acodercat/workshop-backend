"""Open credit holds that the settle path closes out.

One row = one "I might spend up to X µ-credits on this turn / sandbox"
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

V1 only writes ``kind='TURN'`` rows. ``SANDBOX`` is in the enum so
the V2 sandbox-seconds integration is purely additive (no migration).
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    TIMESTAMP,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CreditReservationKind(StrEnum):
    """What's being held.

    - ``TURN``    — a single LLM agent turn. ``session_id`` / ``turn_id``
      populated; ``sandbox_id`` NULL.
    - ``SANDBOX`` — reserved for V2; charge for sandbox container
      runtime. ``sandbox_id`` populated; ``turn_id`` NULL.
    """

    TURN = "TURN"
    SANDBOX = "SANDBOX"


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
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False,
    )
    kind: Mapped[CreditReservationKind] = mapped_column(
        SAEnum(
            CreditReservationKind,
            name="credit_reservation_kind",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # ``SET NULL`` rather than ``CASCADE`` — if a session is deleted,
    # keep the reservation history for audit; it just loses the
    # session-back link.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session.id", ondelete="SET NULL"), nullable=True,
    )
    # No FK on ``turn_id`` / ``sandbox_id`` — those tables ``CASCADE``
    # delete from their parents (session / its own lifecycle), and a
    # broken back-link is preferable to a deleted reservation row.
    turn_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sandbox_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reserved: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[CreditReservationState] = mapped_column(
        SAEnum(
            CreditReservationState,
            name="credit_reservation_state",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=CreditReservationState.OPEN,
        server_default=text("'OPEN'::credit_reservation_state"),
    )
    # Informational in V1 — no janitor sweeps on this column yet. V2's
    # cleanup loop will use it to find abandoned reservations from
    # worker crashes. ``timezone=True`` keeps the tz-aware datetime
    # written by reserve.py round-trip correctly (the bare ``TIMESTAMP``
    # type silently strips tz on write and yields naive on read, which
    # breaks any Python-side comparison against ``datetime.now(utc)``).
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
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
        # Used by the SSE-side settle hook: "find the OPEN reservation
        # for this session". Also partial — historical settled rows
        # don't need to be indexed.
        Index(
            "ix_credit_reservation_open_session",
            "session_id",
            postgresql_where=text("state = 'OPEN'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CreditReservation(id={self.id}, user_id={self.user_id}, "
            f"kind='{self.kind.value}', state='{self.state.value}', "
            f"reserved={self.reserved})>"
        )
