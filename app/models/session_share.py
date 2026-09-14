"""SessionShare — public read-only share link for a session.

One row = one share link. ``token`` is the URL-safe identifier that the
share recipient pastes into their browser; ``password_hash`` (when
present) gates content download behind a bcrypt-checked passphrase.

Policy: at most one share per session. ``POST /sessions/{id}/share``
hard-deletes any prior row before inserting the new one (atomically),
so revocation is a real DELETE, not a flag flip.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SessionShare(Base):
    __tablename__ = "session_share"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``secrets.token_urlsafe(32)`` produces 43-character base64url.
    # UNIQUE prevents collisions and gives us the read-path index for
    # free (lookup is by token — the URL the recipient holds).
    token: Mapped[str] = mapped_column(String(43), unique=True, nullable=False)
    # Bcrypt $2b$ hashes are 60 chars; allow a small margin so a
    # future bcrypt variant (or a different KDF) doesn't require a
    # column-widening migration.
    password_hash: Mapped[str | None] = mapped_column(String(72), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    # Postgres doesn't auto-index FK columns; without this CASCADE
    # delete on the parent session would seq-scan ``session_share``.
    __table_args__ = (Index("ix_session_share_session_id", "session_id"),)

    @property
    def has_password(self) -> bool:
        """Wire-shape projection of ``password_hash IS NOT NULL`` so the
        hash itself never has to leave the database. Pydantic reads it
        through ``from_attributes=True`` on :class:`ShareResponse`."""
        return self.password_hash is not None

    def __repr__(self) -> str:
        return f"<SessionShare(id={self.id}, session_id={self.session_id})>"
