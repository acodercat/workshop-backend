"""Actor — a user-owned, persistent AI worker identity (数字员工).

An actor is the durable *identity* a session runs under: a name plus a
human-facing ``description`` and a model-facing ``instructions`` block.
Everything else an actor "has" lives in tables that reference it, each
with its own lifecycle — tasks are ``session`` rows (via
``session.actor_id``), reference files are ``actor_file`` rows, work
records are ``session_record`` rows. This table deliberately stays that
narrow; per-run preferences (tier, agent type, thinking) are *not*
identity and stay on ``session``.

Always owned (``user_id`` NOT NULL) — unlike ``skill`` / ``recipe``
there is no platform origin tier, because an actor accumulates private
state (memory, files, work history) and a shared instance would leak it
across users. Platform-shipped "官方数字员工" are a future *template*
entity that clones into a user-owned row, not a nullable owner here.

Soft-deleted via ``deleted_at`` (mirrors ``session``): an actor's task
history carries billing and audit weight, so the row must survive
"deletion". Every user-facing read folds ``deleted_at IS NULL`` into
its filter; uniqueness of ``(user_id, name)`` is enforced only over
live rows so a deleted actor's name can be reused.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class Actor(Base):
    __tablename__ = "actor"

    # UUID like ``session`` (not int like ``skill``): the id reaches
    # URLs, OSS keys (avatar), and — once per-actor workspaces land —
    # volume names, where unguessable and cross-environment-unique both
    # matter.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # NOT NULL on purpose — see the module docstring. ``ON DELETE
    # CASCADE`` matches the owned-content user FKs (``session``,
    # ``skill``): deleting a user hard-deletes their actors.
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # What the user calls it — the display name and (later) @handle.
    # Unique per owner over live rows via ``uq_actor_user_id_name``.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # One-liner for humans: list cards, pickers. Never enters the
    # prompt — that is ``instructions``' job, and keeping the two
    # audiences in separate columns is the point of having both.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # The actor's charter, for the model: role, duties, boundaries,
    # style. Composed into the agent's system prompt at turn time
    # (snapshotted onto the turn at enqueue, like ``session_turn.recipe``,
    # so an edit never changes an already-queued turn).
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    # OSS object key of an uploaded avatar image; NULL = no upload, the
    # client derives a generated avatar from ``id`` (same boring-avatars
    # approach as ``user``). A *key*, never a URL (URLs are derived at
    # read time — the ``session_site`` rule). Replacing the avatar means
    # writing a fresh key (cache-busting) and deleting the old object;
    # ``unique=True`` keeps object ownership 1:1 so that delete is safe.
    avatar_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Soft-delete tombstone, same contract as ``session.deleted_at``:
    # NULL = live; non-NULL = deleted (row kept for history/audit,
    # invisible to the user). Only ever tested ``IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        # Name uniqueness per owner, live rows only — a plain UNIQUE
        # would block "delete Alice, create a new Alice" forever.
        Index(
            "uq_actor_user_id_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Backs the ``GET /actors`` list (filter by user_id, ORDER BY
        # updated_at DESC). Partial like ``ix_session_user_id_updated_at``
        # and for the same reason: the only consumer always filters out
        # soft-deleted rows.
        Index(
            "ix_actor_user_id_updated_at",
            "user_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Actor(id={self.id}, name={self.name!r})>"
