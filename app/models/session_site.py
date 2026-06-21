"""SessionSite — a static site published from a session via ``publish_site``.

One row per successful publish. The worker
(``agent-worker/agent_worker/runtime/runner.py``) inserts a row in a
short transaction right after the site's files land in the OSS preview
bucket, keyed by an unguessable ``slug`` that doubles as the publish
**subdomain label** and the **OSS key prefix** (objects live at
``{slug}/...``). The public URL is ``https://{slug}.{preview_domain}/``
and is *derived* from ``slug`` — never stored — so a domain change can't
strand a stale column.

``user_id`` is denormalized from the producing session's owner (an
immutable relationship, so it can never go stale) to make the per-user
"my published sites" query a single indexed scan with no join.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    TIMESTAMP,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SessionSite(Base):
    __tablename__ = "session_site"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Unguessable publish identifier — the subdomain label AND the OSS key
    # prefix. 16-char base36 (lowercase: DNS labels are case-insensitive),
    # capped at the 63-char DNS label limit.
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized from ``session.user_id``; nullable to mirror it, though a
    # claimed turn always carries an owner so published rows have one in
    # practice. Owner is immutable ⇒ this copy never goes stale.
    # ``ON DELETE CASCADE`` matches the session-ownership family.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    # Human-facing name (the ``publish_site`` ``title`` arg, capped at 80
    # chars there) — kept for a future management UI.
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # "My published sites": by owner, newest first.
        Index(
            "ix_session_site_user_id_created_at",
            "user_id",
            text("created_at DESC"),
        ),
        # Postgres doesn't auto-index FK columns; needed so a CASCADE delete
        # on the parent session doesn't seq-scan this table.
        Index("ix_session_site_session_id", "session_id"),
    )

    def __repr__(self) -> str:
        return f"<SessionSite(id={self.id}, slug={self.slug!r})>"
