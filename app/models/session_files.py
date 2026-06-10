"""SessionFile — first-class binding between an OSS file_key and the
session that owns it.

Backs file-download authorization: every file_key the backend will
ever mint a presigned URL for must have a row here. Two write paths,
one read path:

- **User uploads**: ``services/session/enqueue.py#queue_user_message``
  inserts a row in the same transaction as the user message.
- **Tool outputs**: the worker
  (``agent-worker/agent_worker/runtime/runner.py``) inserts rows in a
  short transaction *before* emitting the SSE ``tool_result`` event,
  so the frontend can immediately download the freshly produced file.
- **Read**: download endpoints (``endpoints/session.py`` for owner,
  ``endpoints/share.py`` for share recipients) verify
  ``(session_id, file_key)`` is present here, no JSONB scans, no key-
  prefix parsing.

The presentation copy (filename / mime_type / size_bytes) lives on
the message blocks JSON; this table stays lean — the FK invariant plus
an ``origin`` tag (which file-library bucket the row belongs to, see
``SessionFileOrigin``), not presentation-metadata storage.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    TIMESTAMP,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SessionFileOrigin(StrEnum):
    """Where a ``session_files`` row came from — drives which files the
    cross-session library surfaces (``GET /files`` lists ``UPLOAD`` +
    ``DELIVERABLE``, hides ``INTERNAL``).

    - ``UPLOAD``      — a file the user attached to a message.
    - ``DELIVERABLE`` — a tool output handed to the user (``deliver_file``,
      a published site, …).
    - ``INTERNAL``    — agent-side artifacts not meant for the library:
      ``read_image`` inputs the model looked at, the office→PDF preview
      sidecars (``{name}.preview.pdf``), and similar intermediates.
    """

    UPLOAD = "UPLOAD"
    DELIVERABLE = "DELIVERABLE"
    INTERNAL = "INTERNAL"


class SessionFile(Base):
    __tablename__ = "session_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        nullable=False,
    )
    # OSS keys are globally unique by construction (user uploads use
    # ``session-files/{uuid4}/...`` — older rows still have the
    # legacy ``documents/...`` prefix; tool outputs use
    # ``outputs/{session_uuid}/{tool_use_id}/{idx}_{filename}``). The
    # UNIQUE constraint enforces that and gives us the read-path index
    # for free.
    file_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Which bucket this file belongs to (see ``SessionFileOrigin``).
    # Stamped by the producer: the worker tags by bind path (output →
    # DELIVERABLE, input + preview keys → INTERNAL); the backend's upload
    # path tags UPLOAD.
    origin: Mapped[SessionFileOrigin] = mapped_column(
        SAEnum(
            SessionFileOrigin,
            name="session_file_origin",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    # Postgres doesn't auto-index FK columns; without this CASCADE
    # delete on the parent session would seq-scan ``session_files``.
    __table_args__ = (
        Index("ix_session_files_session_id", "session_id"),
    )

    def __repr__(self) -> str:
        return f"<SessionFile(id={self.id}, file_key={self.file_key!r})>"
