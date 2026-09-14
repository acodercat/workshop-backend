import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import TIMESTAMP, Enum as SAEnum, Index, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SandboxStatus(StrEnum):
    """Sandbox lifecycle state.

    - ``LIVE`` — default after ``create``; the container is up and
      usable (the sandbox/container lifecycle, not turn state).
    - ``PAUSED`` — a live-but-suspended sub-state, paused between turns
      to save resources (OpenSandbox ``pause``/``resume``). No code path
      writes it yet.
    - ``REAPING`` — claimed by the sandbox reaper, kill in flight. A
      transient state the reaper flips a row into *before* it kills the
      container (re-asserting the idle predicate atomically), so a new
      turn can't reconnect to a container that's about to die. The
      worker's ``claim_next_turn`` excludes REAPING; a row stuck here
      (reaper crash between claim and ``TERMINATED``) is retried on the
      next sweep since ``last_used_at`` stays old. Ends at ``TERMINATED``.
    - ``TERMINATED`` — irreversible; the container has been killed.
    """

    LIVE = "LIVE"
    PAUSED = "PAUSED"
    REAPING = "REAPING"
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
        # ``values_callable`` so the Postgres enum holds the ``.value``
        # strings, matching every other SAEnum in the schema (names ==
        # values here, so this only aligns the convention).
        SAEnum(
            SandboxStatus,
            name="sandbox_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=SandboxStatus.LIVE,
    )
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_used_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # The generation of the session's ``/workspace`` storage this container
    # mounted (``session.workspace_generation`` at the time). Worker-owned:
    # written on INSERT, read by ``claim_next_turn`` so a turn reconnects
    # only to a container on the session's current generation. Migration
    # ``a3f9c1d7e2b4``.
    workspace_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # Worker's ``claim_next_turn`` runs a per-claim subquery against
    # ``WHERE session_id = ? AND status <> 'TERMINATED' ORDER BY
    # created_at DESC LIMIT 1`` — index keeps that fast even at scale.
    #
    # The sandbox reaper's ``select_reapable`` scans the other axis:
    # ``WHERE status <> 'TERMINATED' AND last_used_at < ... ORDER BY
    # last_used_at``. A *partial* index on the live set keeps that cheap
    # as ``TERMINATED`` rows accumulate unbounded (they're never deleted):
    # only non-terminal rows are indexed — a tiny fraction — and it serves
    # the predicate, the range, and the ordering in one. Built
    # ``CONCURRENTLY`` in the migration so it doesn't lock the hot insert.
    __table_args__ = (
        Index("ix_sandbox_session_id", "session_id"),
        Index(
            "ix_sandbox_reapable",
            "last_used_at",
            postgresql_where=text("status <> 'TERMINATED'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Sandbox(id={self.id}, session_id={self.session_id}, status={self.status.value})>"
