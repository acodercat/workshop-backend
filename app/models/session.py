import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
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


class SessionStatus(StrEnum):
    """Lifecycle status of a session.

    Five values, three lifecycle layers:

    - ``IDLE`` — initial state stamped by ``create_session`` on a fresh
      row that hasn't had any turn queued yet. Backend's only "start"
      state; the worker never writes IDLE. Stays IDLE until the first
      ``queue_user_message`` flips it to ``RUNNING``.
    - ``RUNNING`` — backend flipped here when a turn was enqueued. The
      worker is (or will be) processing the agent loop.
    - One of three terminals, written by the worker atomically with
      the assistant row insert (or with no row, on the error path):
        - ``COMPLETE`` — agent finished naturally (any non-cancel
          ``stop_reason`` from axon).
        - ``CANCELLED`` — user invoked ``POST /sessions/{id}/cancel``
          and axon's ``stop_reason`` came back ``"cancelled"``. The
          truncated assistant message (whatever was generated before
          the cancel) is still saved.
        - ``FAILED`` — error path or recovery sweep on a dead lease.

    Member values match the Postgres ``session_status`` enum and the
    worker's ``agent_worker.infra.database.SessionStatus`` — closed
    three-way contract over Python / DB / wire.

    ``StrEnum`` (not ``str + Enum``) so ``str(SessionStatus.RUNNING)``
    returns ``"RUNNING"``. The plain ``str + Enum`` mixin inherits
    ``Enum.__str__`` which returns ``"SessionStatus.RUNNING"`` —
    invisible until something stringifies the value into raw SQL or
    a wire payload.
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AgentType(StrEnum):
    """Which kind of agent a session runs.

    - ``FULL`` — sandbox-backed agent (code execution, file tools, skills).
    - ``LITE`` — sandbox-free Q&A + web search.

    Stamped at ``create_session``; the only mutation is the one-way
    LITE → FULL upgrade riding a message send (``queue_user_message``,
    committed atomically with the turn — never the reverse, which would
    strand the session's sandbox and files). Safe to flip between turns
    because the worker re-reads it at every claim to pick the agent
    harness, and the stored conversation context is mode-agnostic.
    Member values match the Postgres ``agent_type`` enum and the
    worker's ``agent_worker.infra.database.AgentType`` — a closed
    Python / DB contract. UPPERCASE matches the ``session_status`` /
    ``sandbox_status`` convention.
    """

    FULL = "FULL"
    LITE = "LITE"


class Tier(StrEnum):
    """Capability tier the user picks for a session — the product-facing
    "how powerful" choice, decoupled from any vendor model name.

    - ``FAST`` — quick responses for everyday chat / light tasks.
    - ``STANDARD`` — balanced speed and capability (the default).
    - ``ADVANCED`` — strongest reasoning / long-task capability.

    Stamped at ``create_session`` alongside the resolved ``model_id``
    (``settings.session_tier_models[tier]``); the tier round-trips to the
    UI while ``model_id`` stays the server-side execution / billing truth.
    UPPERCASE matches the ``agent_type`` / ``session_status`` convention.
    """

    FAST = "FAST"
    STANDARD = "STANDARD"
    ADVANCED = "ADVANCED"


class Session(Base):
    __tablename__ = "session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Nullable for anonymous sessions. ``ON DELETE CASCADE`` so deleting a
    # user wipes their sessions (and, via the session cascade, the derived
    # message / file / site rows) — matching the owned-content user FKs.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    # Sandbox association lives on the inverse side: each ``sandbox`` row
    # carries the session_id it serves. Look up the current sandbox via
    # ``WHERE session_id=? AND status<>'TERMINATED' ORDER BY created_at
    # DESC LIMIT 1`` (worker's ``claim_next_turn`` does exactly that).
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Model id used for this session's agent turns. Resolved at creation
    # from the picked ``tier`` via ``settings.session_tier_models``; never
    # mutated. No Python-side ``default=`` — the endpoint passes it
    # explicitly so forgetting to wire config surfaces as an IntegrityError
    # immediately rather than silently writing a stale model name. Read by
    # the worker (agent build) and credits (billing); the UI never sees it.
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Capability tier the user picked (FAST / STANDARD / ADVANCED). Stamped
    # at creation like ``model_id`` and round-tripped to the UI for the
    # session badge; ``model_id`` above is the tier resolved through
    # ``settings.session_tier_models``. No Python-side ``default=`` —
    # ``create_session`` sets it explicitly (migration backfilled existing
    # rows to STANDARD).
    tier: Mapped[Tier] = mapped_column(
        SAEnum(
            Tier,
            name="session_tier",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # Whether the agent's thinking is on for this session (surfaced to users
    # as "deep think"). Stamped at creation from the composer toggle (default
    # on); the worker reads it at claim time to toggle the model's thinking
    # config. No Python-side ``default=`` — ``create_session`` sets it
    # explicitly (the migration backfilled existing rows to FALSE).
    thinking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Which agent kind this session runs (FULL = sandbox agent, LITE =
    # Q&A + web search). Stamped at creation like ``model_id``; the only
    # mutation is the one-way LITE → FULL upgrade riding a message send
    # (see the ``AgentType`` docstring). The worker reads it at claim
    # time (JOINed onto the turn) to pick the harness. No Python-side
    # ``default=`` — ``create_session`` sets it explicitly (the migration
    # backfilled existing rows to FULL).
    agent_type: Mapped[AgentType] = mapped_column(
        SAEnum(
            AgentType,
            name="agent_type",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # No ``default=`` / ``server_default=`` — every INSERT site has to
    # set status explicitly. There is exactly one INSERT site
    # (``create_session``) and it picks ``IDLE``. A fresh-session
    # default of ``COMPLETE`` (the prior shape, only there to satisfy
    # NOT NULL) was dishonest: a session with zero turns is not
    # "complete". The IDLE state plus an explicit set at the call site
    # makes every row's status describe what actually happened.
    status: Mapped[SessionStatus] = mapped_column(
        # ``values_callable`` keeps the Postgres enum holding the
        # ``.value`` strings (default would use Python member names,
        # which here are identical but the explicit form documents the
        # contract and survives any future enum rename).
        SAEnum(
            SessionStatus,
            name="session_status",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Soft-delete tombstone. NULL = live; non-NULL = deleted (row kept
    # for billing/audit, invisible to the user). Only ever tested
    # ``IS NULL``. Like every timestamp column in this schema, tz-aware
    # (the legacy naive created_at/updated_at were migrated to
    # ``timestamptz`` in revision c7d2f9a4e1b8).
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # The session's ``/workspace`` storage, by generation — owned entirely
    # by the worker (its sandbox reaper writes these, its turn claim reads
    # them); the backend declares them so the model matches the schema and
    # touches none of them. A fresh container mounts storage named for
    # ``workspace_generation``; the reaper moves an idle session to the next
    # generation in the statement that selects its storage for deletion, so
    # the storage being deleted is one no turn can reach. Generation 0 is the
    # pre-generation layout. ``workspace_reclaimed_generation`` is the
    # generation up to which (exclusive) the older storage is confirmed gone
    # — behind ``workspace_generation`` while a deletion is still pending;
    # ``workspace_reclaim_attempted_at`` orders those retries fairly.
    # Migration ``a3f9c1d7e2b4``.
    workspace_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    workspace_reclaimed_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    workspace_reclaim_attempted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # Backs the hot ``GET /sessions`` query (filter by user_id, ORDER BY
    # updated_at DESC). Postgres does not auto-index foreign keys, so
    # without this both the page query and its count fall back to seq
    # scans. Partial on ``deleted_at IS NULL`` — the only consumer is
    # the active-sessions list, which always filters out soft-deleted
    # rows. Migrations ``b74c1e2f9a30`` (original) and the soft-delete
    # rev (partial swap) build it CONCURRENTLY in prod.
    __table_args__ = (
        Index(
            "ix_session_user_id_updated_at",
            "user_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Session(id={self.id}, status='{self.status.value}')>"
