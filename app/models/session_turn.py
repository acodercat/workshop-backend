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
    # User-initiated cancel: the turn ran but was cut short by user
    # intent. Distinct from COMPLETE so the queue layer records intent
    # honestly, mirroring the parent ``session_status.CANCELLED``. The
    # worker writes it via ``cancel_turn``; tokens are still recorded
    # (partial work is billable).
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TerminationReason(StrEnum):
    """Why a turn reached its terminal status — the *why* to
    :class:`SessionTurnStatus`'s *what*.

    Two orthogonal dimensions: ``status`` drives billing and recovery
    (COMPLETE/CANCELLED settle actuals, FAILED-without-usage refunds);
    this column NEVER drives logic — it is the persisted, queryable
    record of which path ended the turn, for UI copy ("stopped: out of
    credits" vs "cancelled") and operations analytics (how often does
    the budget enforcer fire? how many hard timeouts?).

    Values mirror the worker's
    ``agent_worker.infra.database.TerminationReason`` (the worker is
    the main writer; the backend writes only ``WORKER_LOST`` from
    :func:`~app.services.session.recovery.fail_abandoned_turn`) and ride
    the cancel-channel payload + the ``agent.stopped`` wire event —
    change both repos together.

    Grouped by the status each value accompanies:

    - ``COMPLETE`` — ``COMPLETED`` (incl. the ran-to-a-limit family).
    - ``CANCELLED`` — ``USER_CANCELLED`` (stop button),
      ``INSUFFICIENT_CREDITS`` (budget enforcer),
      ``WORKER_SHUTDOWN`` (graceful drain cancelled in-flight turns).
    - ``FAILED`` — ``MODEL_ERROR`` (unrecoverable provider failure),
      ``TIMEOUT`` (hard wall-clock kill), ``EMPTY_OUTPUT`` ("successful"
      turn with nothing replayable), ``INTERNAL_ERROR`` (unexpected
      exception), ``WORKER_LOST`` (lease expired — crash recovery).

    Stored as plain VARCHAR (``native_enum=False``, no CHECK): a
    descriptive field earns Python-side typing, not a Postgres enum
    whose every new member costs an ``ALTER TYPE`` migration.
    """

    COMPLETED = "completed"
    USER_CANCELLED = "user_cancelled"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    WORKER_SHUTDOWN = "worker_shutdown"
    MODEL_ERROR = "model_error"
    TIMEOUT = "timeout"
    EMPTY_OUTPUT = "empty_output"
    INTERNAL_ERROR = "internal_error"
    WORKER_LOST = "worker_lost"


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
    # CUSTOM overriding a platform skill). The ``id`` pins the lineage;
    # the ``bundle_key`` pins the bytes; ``name`` is kept for log /
    # reminder display so we don't need an extra JOIN to render the
    # LLM-facing message.
    #
    # A collection (0..N): NOT NULL DEFAULT '[]' so "no skills" is the
    # empty array, never NULL — readers iterate without a null-guard.
    # Contrast ``recipe`` below: an optional single value that IS NULL.
    skills: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list,
    )
    # Per-turn recipe snapshot, or NULL. The backend resolves the picked
    # recipe id → its vetted ``instructions`` server-side (the client
    # sends only ``recipe_id``, never raw text) and pins the rendered
    # snapshot here, so a later recipe edit / delete can't strand a
    # queued turn. The worker injects ``instructions`` verbatim as an
    # axon ``<system-reminder>``. Shape:
    #   {"id": int, "display_name": str, "instructions": str}
    #
    # An optional single value (0 or 1): nullable, NULL = "no recipe".
    # No DEFAULT — an empty ``{}`` isn't a valid recipe, so unlike the
    # ``skills`` array above there's no meaningful empty value to default to.
    recipe: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Token usage as reported by axon's ``AgentStoppedEvent.usage``,
    # written by the worker in the same fenced UPDATE that flips
    # ``status`` terminal (see
    # ``agent_worker/infra/turn_repository.py::_terminate_turn``) —
    # every terminal that carries a measurement records it, FAILED
    # included (charge-for-produced-tokens). Stays ``NULL`` only when
    # nothing was measured (worker crash, the runner's finally
    # fallback, pre-V2 rows) — backend's credit settle treats ``NULL``
    # as "refund the reservation": cost we can't measure, no charge.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Interim usage report — cumulative tokens consumed *so far*, updated
    # by the worker's lease heartbeat while the turn runs (OCS-style
    # "interim update"). Best-effort telemetry for the backend's budget
    # enforcer, NEVER read by settle: billing only trusts the fenced
    # terminal ``input_tokens`` / ``output_tokens`` above. ``NULL`` until
    # the first heartbeat of a claimed turn.
    interim_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interim_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # Why the terminal ``status`` was reached — see :class:`TerminationReason`.
    # NULL while PENDING/RUNNING; every terminal write stamps a value
    # (pre-rollout historical FAILED rows stay NULL: their sub-reason is
    # unknowable). Plain VARCHAR on the DB side (no native enum / CHECK) so
    # a new reason is a code change, not a migration.
    termination_reason: Mapped[TerminationReason | None] = mapped_column(
        SAEnum(
            TerminationReason,
            native_enum=False,
            create_constraint=False,
            length=32,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=True,
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
        # DB backstop for "at most one non-terminal turn per session" —
        # the invariant enqueue enforces via the session row lock (409 on
        # ``status == RUNNING``) and both recovery paths rely on when they
        # flip by ``session_id`` alone. A future writer that skips the
        # lock discipline fails here instead of silently double-queueing.
        # Mirrors ``uq_credit_reservation_open_session``.
        Index(
            "uq_session_turn_open_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SessionTurn(id={self.id}, session_id={self.session_id},"
            f" status='{self.status.value}', worker_id={self.worker_id!r})>"
        )
