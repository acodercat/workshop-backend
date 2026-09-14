"""Which files a scheduled task attaches to every session it creates.

The canonical list. Each firing copies it onto the new session's own
``session_files`` rows, which is what the download endpoints authorize
against; the OSS object itself is stored once and bound many times.

Singular ``schedule_file``, matching ``schedule`` / ``schedule_run`` /
``session_turn``. The plural ``session_files`` beside it is the odd one
out in this schema — do not read it as the convention and "fix" this to
match.

Why a table rather than JSONB on ``schedule``: these rows are what keeps
the object alive. A presigned upload is born tagged ``lifecycle=orphan``
and an OSS lifecycle rule deletes it about a day later unless something
clears that tag, so attaching a file has to be a durable, queryable fact —
the same reason ``session_files`` is a table. It also lets the per-user
quota count an attachment that no session has bound yet.

``size_bytes`` is the value OSS reported at bind time, never the client's
claim: a low declaration would slip past both the per-file cap and the
quota, which sums this column.
"""

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class ScheduleFile(Base):
    __tablename__ = "schedule_file"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("schedule.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized from the schedule, like ``session_files.user_id`` is from
    # the session: the quota sums bytes per owner and should not have to join
    # ``schedule`` to do it.
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_key: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Attaching the same file twice is one attachment. Leads with
        # ``schedule_id``, so it also serves every read for one schedule —
        # the form's, and each firing's.
        UniqueConstraint("schedule_id", "file_key", name="uq_schedule_file_schedule_id_file_key"),
        # Postgres does not auto-index FK columns. The quota reads by owner,
        # and deleting a ``user`` row runs a reference check that would
        # otherwise seq-scan this table.
        Index("ix_schedule_file_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<ScheduleFile(id={self.id}, schedule_id={self.schedule_id})>"
