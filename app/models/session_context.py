"""Per-session agent memory — axon's native serialized message list.

The worker rehydrates the ``axon.Agent`` from this row at the start of
each turn and upserts it back at turn end. It carries axon ``Message``
dicts (compaction state, provider-opaque data like thinking signatures,
``preserve_in_compact`` flags) that the projected ``message`` table —
which holds UI blocks for display — cannot round-trip. 1:1 with
``session``.

**Worker-touched, backend-migrated.** Like ``session_turn``, the schema
ships here (the backend owns alembic) but the rows are written and read
only by the worker (raw SQL in
``agent_worker.infra.session_context_repository``); the backend never
reads or writes this table. It replaces the worker's former on-disk
``{sessions_dir}/{session_id}.json`` so a session resumes on any worker
replica, not just the node that ran the previous turn.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SessionContext(Base):
    """axon agent memory for one session (see module docstring)."""

    __tablename__ = "session_context"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Provider pin / audit: records which provider produced this
    # serialized state (thinking signatures etc. are provider-specific).
    # Mirrors ``session.model_id``; informational, not used for routing.
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # axon ``Message`` dicts (``Message.to_dict``). Rewritten whole each
    # turn; compaction bounds the size.
    messages: Mapped[list[dict]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    # The session's current todo checklist (``TodoItem`` dicts). Durable owner
    # of the list: ``todo_write`` is compactable in axon, so its calls can be
    # summarized out of ``messages`` — this separate column is never touched by
    # compaction. The worker rewrites it each turn from ``Agent.todos`` and
    # seeds the next run via ``Agent.run(todos=...)``.
    todos: Mapped[list[dict]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
