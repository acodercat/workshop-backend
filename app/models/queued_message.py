"""A message waiting for its session's running turn to end.

Sent while a turn runs, a message cannot start one — a session has at most
one open turn — so it waits here, where its owner can still reword it, move
it or take it back. It becomes a turn only when it goes out, through the same
``queue_user_message`` a message sent to an idle session takes; until then
nothing is reserved and nothing is bound.

Not a ``session_turn`` status: that table is the worker's claim contract,
and a row the user is still editing is no turn yet.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class QueuedMessage(Base):
    """One waiting message: what ``queue_user_message`` is given when it
    goes out, kept as the client sent it."""

    __tablename__ = "queued_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("session.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Where the message stands in its session's queue, lowest first. Rewritten
    # for the whole queue on a reorder, so it is unique only by convention;
    # ``id`` breaks a tie.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # ``FileRef`` dicts, as on ``session_turn.files``. Not bound to the
    # session until the message goes out.
    files: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    # The skill names the composer picked — names, not the snapshot
    # ``session_turn.skills`` holds: they resolve when the message goes out,
    # as they would for one sent by hand at that moment.
    skills: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    # The client's IANA zone, as on ``session_turn.timezone``.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # When the dispatcher declined to send this message on. While any of a
    # session's messages has one, its queue is held: nothing is sent on
    # until the session's next turn starts, which clears it (see
    # ``app/workers/message_queue_dispatcher.py``). NULL = not held.
    held_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_queued_message_session_id", "session_id", "position"),)

    def __repr__(self) -> str:
        return f"<QueuedMessage(id={self.id}, session_id={self.session_id})>"
