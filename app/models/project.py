"""Projects — a user's named group of sessions, with shared files and instructions.

A session belongs to at most one project (``session.project_id``). Every
session in a project runs under its instructions and sees its files
(``project_file``); the worker reads both when it claims the session's turn.

Soft-deleted like ``session``. Deleting a project moves its sessions out
(``session.project_id`` set to NULL) rather than deleting them: dropping a
grouping is not the same as discarding the conversations in it. So a live
session never points at a deleted project, and no reader of
``session.project_id`` has to join this table to ask.

**``updated_at`` tracks the project, not its sessions' traffic.** It moves
when the project itself changes — a rename, new instructions, a file added
or removed, a session created in it or moved into it — and not when a
message is sent in one of its sessions. That keeps the list ordered by
what the owner did to the project, and keeps every enqueue from writing
and locking a second row. Nor when a session is moved *out*: that is done
from the session, and takes no lock on the project it leaves. Writes to
``project_file`` or ``session`` do not fire this row's ``onupdate``, so
those paths set it explicitly.

**The row is the project's lock.** Every write that changes which sessions
or files belong to a project — deleting it, creating or moving a session
into it, adding or removing a file — takes ``FOR UPDATE`` on this row
first, and only then touches ``session`` rows. One order everywhere, and
enqueue never locks a project, so the order cannot form a cycle.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class Project(Base):
    __tablename__ = "project"

    # A UUID, like ``session.id``: it is the id in ``/projects/{id}``, and a
    # sequence would let anyone count other users' projects.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # What every session in the project runs under, verbatim. Empty when the
    # owner has written none. Its length cap is the API's, not the column's.
    instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # The icon the client draws for the project, by the client's name for it;
    # NULL until the owner picks one, and the client draws its default. Only
    # the name's shape is checked here: which icons exist is the client's
    # catalog, so adding one is a frontend change.
    icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # When the owner pinned the project; NULL = not pinned. A time rather
    # than a flag, like ``session.pinned_at``: pinned projects list most
    # recently pinned first. Pinning is not a change to the project, so it
    # leaves ``updated_at`` alone.
    pinned_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Soft-delete tombstone. NULL = live.
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        # Backs ``GET /projects`` — one owner's live projects, most recently
        # updated first — and the per-user cap's count. Partial because both
        # only ever ask about live rows.
        Index(
            "ix_project_user_id_updated_at",
            "user_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Project(id={self.id}, user_id={self.user_id})>"
