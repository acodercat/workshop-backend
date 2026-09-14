"""Which MCP servers a scheduled task's sessions are configured to use.

The schedule-level twin of :mod:`app.models.session_mcp_server`, and it
exists for the same reason that one does: these rows point at *live*
``mcp_server`` rows, so ``ON DELETE CASCADE`` has to remove a deleted
Connector from every schedule that named it. An id array on ``schedule``
could not do that, and remembering to clean up at each deletion path is
the thing a foreign key exists to avoid.

The two tables stay separate rather than one polymorphic binding: a
session's Connectors are edited live by the composer switches while a
turn may already be running, a schedule's are edited in a form and only
read when it fires. Same shape, unrelated lifetimes.

Read once per firing, in :func:`app.workers.schedule_dispatcher._enqueue`,
and handed to ``build_session`` — which writes the survivors into the new
session's own ``session_mcp_server`` rows. From there the ordinary
per-turn path takes over: each enqueue re-resolves them against the live
rows (still owned, still connected) and snapshots what is left into
``session_turn.mcp_servers``. So a Connector the owner disconnected
between two firings simply stops appearing, with no write here.

Order is not kept, matching the session table: nothing downstream depends
on it, and the worker connects by its own rule (failed-last).
"""

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class ScheduleMCPServer(Base):
    __tablename__ = "schedule_mcp_server"

    schedule_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("schedule.id", ondelete="CASCADE"),
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
        # The composite PK leads with ``schedule_id``, so it cannot serve the
        # ``ON DELETE CASCADE`` from ``mcp_server`` — the path that made this a
        # table rather than an array. Postgres does not auto-index FK columns,
        # so without this every Connector deletion seq-scans.
        Index("ix_schedule_mcp_server_mcp_server_id", "mcp_server_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduleMCPServer(schedule_id={self.schedule_id}, "
            f"mcp_server_id={self.mcp_server_id})>"
        )
