"""User-submitted feedback (bug reports / suggestions) addressed to
the admin team.

Distinct from :mod:`app.models.message_feedback`, which captures
per-AI-response thumbs-up / thumbs-down ratings. This is application-
level feedback the user proactively sends, with optional image
attachments uploaded through the standard presigned-OSS flow before
the row is created.
"""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    TIMESTAMP,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class FeedbackStatus(StrEnum):
    """Admin-side workflow state.

    - ``OPEN`` (default) — newly submitted, awaiting review.
    - ``RESOLVED`` — admin has addressed it (fix shipped /
      question answered / suggestion adopted).
    - ``IGNORED`` — admin dismissed (spam / duplicate / out of
      scope).

    Values match the Postgres ``feedback_status`` enum and the Python
    member names — same convention as :class:`UserStatus`.
    """

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"


class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ``ON DELETE SET NULL`` keeps the row after a user is deleted so
    # admins still have an audit trail; the relationship simply
    # becomes ``None`` on the orphaned row.
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Free-text description. The 2000-char ceiling matches the
    # frontend's ``MESSAGE_MAX_LENGTH``; the service layer trims
    # whitespace before persist.
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    # OSS file keys of pre-uploaded images. Empty list when no
    # attachments. Stored as ``JSONB`` (not ``TEXT[]``) for parity
    # with other "list of structured items" columns in the project,
    # and to leave room for future per-attachment metadata without a
    # column-shape migration. The 5-image cap is enforced at the
    # endpoint layer (UX limit, not a data-integrity rule).
    image_keys: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    status: Mapped[FeedbackStatus] = mapped_column(
        SAEnum(
            FeedbackStatus,
            name="feedback_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=FeedbackStatus.OPEN,
        server_default=FeedbackStatus.OPEN.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped["User | None"] = relationship(back_populates="feedback_submissions")

    __table_args__ = (
        # Admin "review queue" query: filter status, sort newest first.
        Index("ix_user_feedback_status_created", "status", "created_at"),
        # Per-user history (rare but cheap; NULL rows for orphaned
        # feedback are skipped by Postgres B-tree NULL handling).
        Index("ix_user_feedback_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<UserFeedback(id={self.id}, user_id={self.user_id}, status={self.status.value})>"
