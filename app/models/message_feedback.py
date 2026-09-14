"""MessageFeedback — per-user thumbs-up / thumbs-down on a message.

One row = one user's feedback on one message. ``UNIQUE(message_id,
user_id)`` makes "one user, one vote per message" a DB-enforced
invariant; flipping the rating or editing the comment upserts the row
in place rather than producing audit history.

``rating`` is a Postgres native enum (``UP`` / ``DOWN``) rather than
a numeric encoding — the wire shape uses the same uppercase strings,
so there's no encoding boundary to translate at the endpoint. Adding
a third option later means an ``ALTER TYPE ... ADD VALUE`` migration,
acceptable since binary feedback rarely grows.

``comment`` is nullable — most votes won't have one. Application
layer caps the length at 1000 chars; the column is plain ``TEXT`` to
avoid a column-widening migration if the cap moves.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class FeedbackRating(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class MessageFeedback(Base):
    __tablename__ = "message_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # BigInteger to match ``message.id`` (widened together in migration
    # ``d5b1f7c3a9e2``).
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("message.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[FeedbackRating] = mapped_column(
        SAEnum(
            FeedbackRating,
            name="message_feedback_rating",
            # Persist by value, matching the schema-wide native-enum
            # convention (names == values, so purely convention alignment).
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # ``message_id`` leads, so this constraint's backing index also
        # serves the FK CASCADE scan from ``message`` and every
        # message-scoped read — no separate single-column index needed
        # (the redundant ``ix_message_feedback_message_id`` was dropped
        # in migration ``e1c5a9d7b3f4``).
        UniqueConstraint("message_id", "user_id", name="uq_message_feedback_user"),
    )

    def __repr__(self) -> str:
        return (
            f"<MessageFeedback(id={self.id}, message_id={self.message_id}, "
            f"rating={self.rating.value})>"
        )
