"""InviteRedemption — one row per account created via an invite code.

The source-of-truth audit behind the cached
:attr:`~app.models.invite_code.InviteCode.used_count`. Append-only
like ``credit_ledger``: a redemption is a historical fact, so the
table carries only ``created_at`` and is never UPDATEd.

``UNIQUE(invitee_user_id)`` makes "a user is invited at most once" a
DB-enforced invariant. ``referrer_user_id`` denormalizes the code's
owner at redemption time (NULL for SYSTEM codes) so the referral edge
is queryable both ways without joining through ``invite_code`` —
"who invited me" by ``invitee_user_id``, "who did I invite" by
``referrer_user_id``.

All three FKs are ``RESTRICT``: redemptions are provenance, so the
parent user / code can't be silently deleted out from under them
(same posture as ``credit_ledger``). Real deletion is a deliberate
ops step.
"""

from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class InviteRedemption(Base):
    __tablename__ = "invite_redemption"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    invite_code_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("invite_code.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invitee_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Denormalized ``invite_code.owner_user_id`` snapshot. NULL when the
    # code was a SYSTEM code (no referrer).
    referrer_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "invitee_user_id", name="uq_invite_redemption_invitee",
        ),
        # FK read-path indexes (Postgres doesn't auto-index FK columns):
        # per-code redemption count, and the referrer's "my invitees"
        # list.
        Index("ix_invite_redemption_invite_code_id", "invite_code_id"),
        Index("ix_invite_redemption_referrer_user_id", "referrer_user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<InviteRedemption(id={self.id}, "
            f"invite_code_id={self.invite_code_id}, "
            f"invitee_user_id={self.invitee_user_id})>"
        )
