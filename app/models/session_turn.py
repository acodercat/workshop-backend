"""DB lease ownership row for one queued turn.

The backend INSERTs a row with ``status='PENDING'`` per user message;
the worker atomically claims it (``FOR UPDATE SKIP LOCKED + UPDATE``),
holds the lease via periodic renew, and writes the terminal state on
success / failure. Schema lives in
``alembic/versions/b8d2f4a6c7e9_rename_session_run_to_session_turn.py``.

Status enum string values **mirror** the worker's
``agent_worker.infra.database.SessionTurnStatus`` member names —
UPPERCASE, matching the project convention used by :class:`Sandbox.status`
/ :class:`Message.role`. Any change must land in both repos atomically
or claim/recovery silently no-op.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    TIMESTAMP,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SessionTurnStatus(StrEnum):
    """Lifecycle status of a ``session_turn`` row.

    Member values match the Postgres ``session_turn_status`` enum and
    the worker's ``agent_worker.infra.database.SessionTurnStatus`` —
    the wire contract is a closed three-way agreement.

    ``StrEnum`` (not ``str + Enum``) so ``str(...)`` returns the value;
    see :class:`SessionStatus` for the rationale.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class SessionTurn(Base):
    """One queued turn. Joins to ``session`` for ``user_id``,
    ``model_id`` — those used to be cached here but are read live from
    the parent (single source of truth). Sandbox association lives on
    the inverse side: ``sandbox.session_id``."""

    __tablename__ = "session_turn"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        nullable=False,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    files: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list,
    )
    # Per-turn skill selection. Each element is a snapshot
    # ``{"id": int, "name": str, "bundle_key": str}`` taken at enqueue
    # time so the worker loads the exact bundle the user picked, even
    # if the row is later mutated (delete / re-upload / a same-named
    # CUSTOM overriding a BUILTIN). The ``id`` pins the lineage; the
    # ``bundle_key`` pins the bytes; ``name`` is kept for log / reminder
    # display so we don't need an extra JOIN to render the LLM-facing
    # message.
    skills: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list,
    )
    # Token usage as reported by axon's ``AgentStoppedEvent.usage``,
    # written by the worker in the same fenced UPDATE that flips
    # ``status`` to ``COMPLETE`` / ``CANCELLED`` (see
    # ``agent_worker/infra/turn_repository.py::_terminate_turn``).
    # Stays ``NULL`` on the ``FAILED`` path and on pre-V2 historical
    # rows — backend's credit settle treats ``NULL`` as "refund the
    # reservation": no value delivered, no charge.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[SessionTurnStatus] = mapped_column(
        # ``values_callable`` keeps the Postgres enum holding the
        # ``.value`` strings (default would use Python member names,
        # which here are identical but the explicit form documents the
        # contract and survives any future enum rename).
        SAEnum(
            SessionTurnStatus,
            name="session_turn_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=SessionTurnStatus.PENDING,
    )
    # Worker process id that holds the lease. Cleared by no one — kept
    # on terminal rows for forensics ("which worker's turns keep timing
    # out"); fencing predicates never match on terminal status anyway.
    # ``VARCHAR(255)`` to align with other identity columns
    # (``user.name``, ``session.title``); the default identity shape
    # ``hostname-pid`` is at most ~72 chars, k8s pod-name based
    # identities ~253, so 255 has comfortable headroom while still
    # documenting "this is a short identifier, not free-form text".
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(), onupdate=func.now(), nullable=False,
    )
    # Set to ``now()`` on the terminal write (COMPLETE / CANCELLED /
    # FAILED); NULL while PENDING / RUNNING. Anchors duration math —
    # distinct from ``updated_at``, which lease heartbeats also bump.
    terminated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )

    __table_args__ = (
        # Partial index matching the migration — claim_next_turn scans
        # pending only (the architectural decision is "no auto-retry";
        # expired RUNNING is owned by expire_stale_turns).
        Index(
            "ix_session_turn_claim",
            "status",
            "lease_expires_at",
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
        Index("ix_session_turn_session_id", "session_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<SessionTurn(id={self.id}, session_id={self.session_id},"
            f" status='{self.status.value}', worker_id={self.worker_id!r})>"
        )
