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

import enum
from datetime import datetime

from sqlalchemy import (
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class FeedbackRating(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"


class MessageFeedback(Base):
    __tablename__ = "message_feedback"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("message.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[FeedbackRating] = mapped_column(
        SAEnum(FeedbackRating, name="message_feedback_rating"),
        nullable=False,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "message_id", "user_id", name="uq_message_feedback_user"
        ),
        # Postgres doesn't auto-index FK columns; without this CASCADE
        # delete on the parent message would seq-scan ``message_feedback``.
        # The composite UNIQUE above doubles as the (message_id, user_id)
        # read-path index.
        Index("ix_message_feedback_message_id", "message_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<MessageFeedback(id={self.id}, message_id={self.message_id}, "
            f"rating={self.rating.value})>"
        )
