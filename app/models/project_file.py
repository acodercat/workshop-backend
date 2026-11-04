"""The files a project shares with every session in it.

The worker mirrors a project's files into each of its sessions' sandboxes
at ``/workspace/project/``. Rows reference an OSS object by ``file_key``
rather than copying it, like ``schedule_file``: the object is stored once
and may be bound many times. Nothing deletes a user's objects — a session's
soft delete keeps them — so a file added from a session outlives it.

**One row per version.** Writing a name the project already has adds a row
with the next ``version``; readers show each name's highest live version.
Deleting a file soft-deletes every version of its name — the owner removed
*the file*, not its latest revision — and a later file of the same name
numbers on from the highest version ever written, deleted ones included,
so ``uq_project_file_project_id_filename_version`` never collides.

**Only the owner adds files**, on the project's page or from a session: the
agent reads them and never writes here, since what a project holds is the
user's to decide. A version is allocated under ``FOR UPDATE`` on the
``project`` row, as ``coalesce(max(version), 0) + 1`` over every row of
that ``(project_id, filename)``; the lock makes the read-then-insert safe,
and the unique constraint is the backstop.

``user_id`` is denormalized from the project, as ``schedule_file.user_id``
is from its schedule: the upload quota and ``assert_files_owned_by`` both
look rows up by owner and should not join ``project`` to do it.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class ProjectFileOrigin(StrEnum):
    """Where a project file came from.

    - ``UPLOAD`` — uploaded on the project's page.
    - ``SESSION_FILE`` — added from one of the owner's session files.
    """

    UPLOAD = "UPLOAD"
    SESSION_FILE = "SESSION_FILE"


class ProjectFile(Base):
    __tablename__ = "project_file"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    file_key: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # What OSS reported when the file was added, never a client's claim: the
    # upload quota sums this column.
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[ProjectFileOrigin] = mapped_column(
        SAEnum(
            ProjectFileOrigin,
            name="project_file_origin",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )
    # The session a ``SESSION_FILE`` came from. Provenance, not a foreign
    # key: it must survive the session, and ``SET NULL`` would break
    # ``ck_project_file_origin_session_consistent``. Referenced by value,
    # like ``sandbox.session_id``.
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    # Soft-delete tombstone. NULL = live.
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        # Leads with ``project_id``, so it also serves every read of one
        # project's files: the list, and the next version of a name.
        UniqueConstraint(
            "project_id",
            "filename",
            "version",
            name="uq_project_file_project_id_filename_version",
        ),
        CheckConstraint(
            "(origin = 'UPLOAD') = (origin_session_id IS NULL)",
            name="ck_project_file_origin_session_consistent",
        ),
        # Postgres does not auto-index FK columns. The quota reads by owner,
        # and deleting a ``user`` row runs a reference check that would
        # otherwise seq-scan this table.
        Index("ix_project_file_user_id", "user_id"),
        # ``assert_files_owned_by`` looks keys up across every project, with
        # no project filter — the reason ``ix_session_files_file_key`` exists.
        Index("ix_project_file_file_key", "file_key"),
    )

    def __repr__(self) -> str:
        return f"<ProjectFile(id={self.id}, project_id={self.project_id}, version={self.version})>"
