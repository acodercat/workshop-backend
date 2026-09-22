"""InviteCode — an access code redeemed at registration.

Two origins, mirroring the ``skill`` / ``recipe`` pattern:

- ``SYSTEM`` — operator-issued, no owner (``owner_user_id IS NULL``).
  The cold-start path: the first users have no referrer, so admins
  mint a SYSTEM code (optionally capped / time-boxed) to seed the
  graph.
- ``REFERRAL`` — a user's personal code (``owner_user_id`` set). Every
  ACTIVE user gets exactly one, enforced by the partial-unique index
  below. Registering with it binds the new user to the referrer (see
  :class:`~app.models.invite_redemption.InviteRedemption`).

A valid redemption activates the new account immediately — the code
*is* the access grant, so there is no separate ``grants_status``
column; the registration service always inserts ``status=ACTIVE``.

``used_count`` is a cached counter checked by the fenced redemption
``UPDATE`` (``used_count < max_uses``); ``invite_redemption`` rows are
the source-of-truth audit. Same cache-vs-truth split as
``user.credit_balance`` ↔ ``credit_ledger``.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class InviteCodeOrigin(StrEnum):
    SYSTEM = "SYSTEM"
    REFERRAL = "REFERRAL"


class InviteCode(Base):
    __tablename__ = "invite_code"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    # High-entropy random string so codes can't be enumerated. ``UNIQUE``
    # is the redemption lookup key.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    origin: Mapped[InviteCodeOrigin] = mapped_column(
        SAEnum(
            InviteCodeOrigin,
            name="invite_code_origin",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        server_default=text("'REFERRAL'"),
    )
    # ``RESTRICT`` rather than ``CASCADE`` — a user who has issued a
    # referral code can't be silently deleted (same posture as
    # ``credit_ledger``). It also guarantees a REFERRAL row's owner is
    # never set NULL behind our back, which would break the consistency
    # CHECK below.
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # NULL ⇒ unlimited uses.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    # NULL ⇒ never expires.
    expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    # Free-text operator note, e.g. "beta wave 1". Only meaningful for
    # SYSTEM codes.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # ``origin`` and ``owner_user_id`` must agree — a REFERRAL code
        # with no owner (or a SYSTEM code with one) is nonsense the
        # redemption logic would mis-attribute. Reject at the DB so
        # neither code nor manual SQL can introduce it. Mirrors
        # ``ck_skill_origin_user_id_consistent``.
        CheckConstraint(
            "(origin = 'SYSTEM' AND owner_user_id IS NULL) "
            "OR (origin = 'REFERRAL' AND owner_user_id IS NOT NULL)",
            name="ck_invite_code_origin_owner_consistent",
        ),
        # The fenced redemption UPDATE keeps ``used_count`` within
        # ``max_uses``; this CHECK is the backstop against a concurrent
        # bug or manual SQL overshooting the cap.
        CheckConstraint(
            "used_count >= 0 AND (max_uses IS NULL OR used_count <= max_uses)",
            name="ck_invite_code_used_within_max",
        ),
        # One stable referral code per user. SYSTEM codes (owner NULL)
        # are exempt — NULL ≠ NULL means the partial predicate skips
        # them. Doubles as the "fetch my referral code" lookup index.
        Index(
            "uq_invite_code_referral_owner",
            "owner_user_id",
            unique=True,
            postgresql_where=text("origin = 'REFERRAL'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<InviteCode(id={self.id}, origin={self.origin.value}, code={self.code!r})>"
