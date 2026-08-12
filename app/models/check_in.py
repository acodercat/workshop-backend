"""CheckIn — one row per user per (business-timezone) calendar day the
user claimed the daily check-in reward.

Append-only audit table in the same spirit as ``invite_redemption``: a
check-in is a historical fact, so the row carries only ``created_at``
and is never UPDATEd. ``UNIQUE(user_id, check_in_date)`` makes "at most
one check-in per day per user" a DB-enforced invariant — the first line
of defense against a double-claim on retry / double-click, backing the
``credit_ledger`` idempotency key on the paired grant.

``check_in_date`` is a ``DATE`` (not a timestamp): the "day" is the
calendar day in the business timezone (``settings.checkin_timezone``,
Asia/Shanghai), computed by the service and passed in — a UTC ``now()``
default would put an early-morning check-in on the wrong day.

The reward amount is **not** stored here: it lives in the paired
``CHECKIN`` ``credit_ledger`` row (the source of truth for credit
movement), reached via ``ledger_id`` — a forward back-link mirroring
``withdrawal_request.ledger_id`` / ``payment_order.ledger_id``, and the
same "money lives in the ledger" posture as ``invite_redemption`` (which
likewise stores no amount).

``streak_day`` is the consecutive-day count including this check-in
(``1`` on a fresh start or after a gap) — a write-time snapshot so the
"已连续签到 N 天" display and any future streak rewards never need a
gaps-and-islands scan. Immutable like the rest of the row.

The ``user_id`` / ``ledger_id`` FKs are ``RESTRICT``: this is
provenance, so the parent user / ledger row can't be silently deleted
out from under it (same posture as ``credit_ledger`` /
``invite_redemption``).
"""

from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CheckIn(Base):
    __tablename__ = "check_in"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Calendar day in the business timezone; computed by the service, not
    # a DB default (``now()`` is UTC and would misdate early-morning claims).
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Consecutive-day count including this check-in (1 after a gap / first ever).
    streak_day: Mapped[int] = mapped_column(Integer, nullable=False)
    # Back-link to the paired CHECKIN ``credit_ledger`` row (the reward).
    ledger_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("credit_ledger.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # "One check-in per day per user." The composite index this
        # creates also serves both read paths — the latest-row lookup for
        # the streak calc (backward scan) and per-user history pagination —
        # so no separate index is needed (unlike ``invite_redemption``,
        # whose UNIQUE is on a single column).
        UniqueConstraint(
            "user_id",
            "check_in_date",
            name="uq_check_in_user_date",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CheckIn(id={self.id}, user_id={self.user_id}, "
            f"check_in_date={self.check_in_date}, streak_day={self.streak_day})>"
        )
