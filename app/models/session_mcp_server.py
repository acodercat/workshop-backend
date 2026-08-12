"""Which MCP servers a session is configured to use.

Session-level configuration, like ``session.tier`` and
``session.thinking_enabled``: declared once and applied to every turn rather
than re-sent per message. The composer's Connector switches write it.

A table rather than an id array on ``session`` because these point at *live*
rows. ``ON DELETE CASCADE`` from ``mcp_server`` is what an array cannot have:
deleting a Connector removes it from every session that had it configured,
guaranteed by the database instead of remembered at each deletion path.

Not to be confused with ``session_turn.mcp_servers``, which is JSONB and stays
that way: that column is an immutable snapshot of what one turn actually got,
and a foreign key there would let a later deletion rewrite history. This one is
current intent; that one is the record. Each enqueue reads this, resolves it
against the live rows — still owned, still connected — and writes the survivors
there.

Order is not kept. ``_resolve_mcp_servers`` preserved the caller's order only so
a queued row read the way the user picked; nothing downstream depends on it, and
the worker connects by its own rule (failed-last).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SessionMCPServer(Base):
    __tablename__ = "session_mcp_server"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mcp_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("mcp_server.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # The composite PK leads with ``session_id``, so it cannot serve the
        # ``ON DELETE CASCADE`` from ``mcp_server`` — the path that made this a
        # table rather than an array. Postgres does not auto-index FK columns,
        # so without this every Connector deletion seq-scans.
        Index("ix_session_mcp_server_mcp_server_id", "mcp_server_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<SessionMCPServer(session_id={self.session_id}, mcp_server_id={self.mcp_server_id})>"
        )
