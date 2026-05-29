import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum as SAEnum, Index, String, TIMESTAMP, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SandboxStatus(StrEnum):
    """Sandbox lifecycle state.

    - ``RUNNING`` — default after ``create``; the container is up.
    - ``PAUSED`` — paused between turns to save resources (OpenSandbox
      ``pause``/``resume``).
    - ``TERMINATED`` — irreversible; the container has been killed.
    """

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"


class Sandbox(Base):
    """One OpenSandbox container, scoped to a session.

    Every sandbox row carries the ``session_id`` it was created for.
    Multiple rows can exist per session over time — the reaper kills
    long-idle containers and the worker creates fresh ones on the next
    turn, leaving the older row behind in ``TERMINATED`` status. The
    "active" sandbox for a session at any moment is the most recent
    non-TERMINATED row.

    Cleanup is the reaper's responsibility — the worker never calls
    ``kill()``; it only ``close()``s the HTTP client, leaving the
    container alive for the next turn. A separate reaper process kills
    a container once it is idle past a threshold (by ``last_used_at``)
    or its session is soft-deleted, then flips the row ``TERMINATED``.

    ``session_id`` is application-managed (no DB-level FK) — kept that
    way to avoid CASCADE footguns and constraint cost on the worker's
    hot insert path.
    """

    __tablename__ = "sandbox"

    # OpenSandbox-assigned identifier (what ``Sandbox.connect(id)``
    # needs). Changes if the reaper killed this session's container
    # and the worker had to recreate.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The session this container was created for. Worker writes on
    # INSERT, never updates. No DB-level FK — see class docstring.
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[SandboxStatus] = mapped_column(
        SAEnum(SandboxStatus, name="sandbox_status"),
        nullable=False,
        default=SandboxStatus.RUNNING,
    )
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), nullable=False,
    )
    last_used_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), nullable=False,
    )

    # Worker's ``claim_next_turn`` runs a per-claim subquery against
    # ``WHERE session_id = ? AND status <> 'TERMINATED' ORDER BY
    # created_at DESC LIMIT 1`` — index keeps that fast even at scale.
    __table_args__ = (
        Index("ix_sandbox_session_id", "session_id"),
    )

    def __repr__(self) -> str:
        return f"<Sandbox(id={self.id}, session_id={self.session_id}, status={self.status.value})>"
