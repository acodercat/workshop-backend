import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
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


class Session(Base):
    __tablename__ = "session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )
    # Sandbox association lives on the inverse side: each ``sandbox`` row
    # carries the session_id it serves. Look up the current sandbox via
    # ``WHERE session_id=? AND status<>'TERMINATED' ORDER BY created_at
    # DESC LIMIT 1`` (worker's ``claim_next_turn`` does exactly that).
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Model id used for this session's agent turns. Stamped at creation
    # time from ``settings.default_session_model_id``; never mutated. No
    # Python-side ``default=`` — the endpoint passes it explicitly so
    # forgetting to wire config surfaces as an IntegrityError immediately
    # rather than silently writing a stale model name.
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
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
        TIMESTAMP, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Soft-delete tombstone. NULL = live; non-NULL = deleted (row kept
    # for billing/audit, invisible to the user). Only ever tested
    # ``IS NULL``; tz-aware because that's the correct default for new
    # timestamp columns (created_at/updated_at predate this and use a
    # naive TIMESTAMP — not worth a migration to fix retroactively).
    deleted_at: Mapped[datetime | None] = mapped_column(
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
