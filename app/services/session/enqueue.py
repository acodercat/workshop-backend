"""Enqueue a user message for the worker.

One load-bearing function, :func:`queue_user_message`, sequences
three steps in a specific order:

1. Lock the session row and reject if a live generation is in progress
   (:class:`GenerationInProgressError`). The lock serialises concurrent
   enqueue requests for the same session so the second one always sees
   the first's ``running`` status.
2. In one transaction:
   - DELETE the per-session output stream so the next turn's events
     don't tail onto stale history;
   - INSERT the user ``message`` row;
   - INSERT the ``session_turn`` row (status=``pending``) — this is
     what the worker's ``claim_next_turn`` will pick up;
   - flip ``session.status`` to ``running``.
3. After the DB commits, ``PUBLISH notify:worker`` so an idle worker
   wakes immediately. Best-effort: a missed publish is recovered by
   the worker's poll fallback.

Conversation history is **not** sent — the worker keeps its own
compacted context file.
"""

import logging

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message, MessageRole
from app.models.session import Session, SessionStatus
from app.models.session_turn import SessionTurn, SessionTurnStatus
from app.schemas.session import FileRef
from app.services.redis import (
    SESSION_STREAM_PREFIX,
    publish_wake,
    stream_key,
)
from app.services.session.exceptions import GenerationInProgressError

logger = logging.getLogger(__name__)


async def queue_user_message(
    db: AsyncSession,
    redis: aioredis.Redis,
    session: Session,
    *,
    content: str,
    files: list[FileRef],
) -> None:
    """Persist the user message + session_turn, flip status, wake worker.

    Order is load-bearing:

    1. ``SELECT ... FOR UPDATE`` the session row to serialise concurrent
       enqueues for the same session, then reject if status is already
       ``running``. PostgreSQL holds the lock until the transaction
       commits, so two racing requests pass through one at a time and
       the second sees the first's ``running`` status.
    2. DELETE the previous turn's output stream (lingers post-
       ``agent.stopped`` for late SSE replay; cleared so new events
       don't tail onto stale history). Then insert the user message,
       insert the ``session_turn`` (status=``pending``), and flip
       ``session.status='RUNNING'`` — all in one commit so a refetch
       between steps can't see a half-applied state.
    3. ``publish_wake`` so an idle worker doesn't have to wait for its
       poll cycle. **After commit** so the worker's claim sees the row.

    No XADD failure path: there's no Redis input queue any more. The
    Redis interaction is one async ``PUBLISH`` that is best-effort by
    design (poll fallback covers it).

    ``session.sandbox_id`` is ``None`` on the first turn — the worker
    creates a fresh container and writes the assigned id back.

    Raises:
        GenerationInProgressError: a turn is already streaming.
    """
    session = await _lock_session(db, session.id)
    if session.status == SessionStatus.RUNNING:
        raise GenerationInProgressError(
            "A completion is already in progress for this session"
        )

    # The previous turn's stream lingers post-``agent.stopped`` for
    # late SSE replay; delete before the new run so new events don't
    # tail onto stale history. ``DELETE`` is a no-op on missing keys.
    await redis.delete(stream_key(SESSION_STREAM_PREFIX, session.id))

    file_dicts = [f.model_dump() for f in files]
    user_msg = Message(
        session_id=session.id,
        role=MessageRole.USER,
        blocks=[{"type": "text", "content": content, "files": file_dicts}],
    )
    # ``user_id`` / ``model_id`` / ``sandbox_id`` are intentionally not
    # cached on the run — the worker JOINs ``session`` to read them
    # live, so the parent row is the single source of truth.
    session_turn = SessionTurn(
        session_id=session.id,
        query=content,
        files=file_dicts,
        status=SessionTurnStatus.PENDING,
    )
    session.status = SessionStatus.RUNNING
    db.add(user_msg)
    db.add(session_turn)
    await db.commit()  # releases the FOR UPDATE lock + makes session_turn visible

    # Wake any idle worker — fire-and-forget; if it fails the worker's
    # poll fallback (5s) catches the row anyway. Logging only, no raise:
    # the user-visible effect (message stored, session running) already
    # committed.
    try:
        await publish_wake(redis, session.id)
    except Exception:
        logger.warning(
            "publish_wake failed for session %s — worker poll fallback will catch it",
            session.id, exc_info=True,
        )


async def _lock_session(db: AsyncSession, session_id: str) -> Session:
    """Re-fetch the session under a row-level lock.

    Returns the locked row whose ``status`` is the authoritative answer
    to "is a generation running for this session"; the lock is held
    until the surrounding transaction commits or rolls back. The
    caller-supplied ``Session`` object may be a stale snapshot (read
    in a different transaction at endpoint dispatch); the lock is what
    makes the subsequent status check race-free.
    """
    result = await db.execute(
        select(Session).where(Session.id == session_id).with_for_update()
    )
    return result.scalar_one()
