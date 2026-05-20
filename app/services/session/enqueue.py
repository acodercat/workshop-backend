"""Enqueue a user message for the worker.

One load-bearing function, :func:`queue_user_message`, sequences
the steps in a specific order:

1. Lock the session row and reject if a live generation is in progress
   (:class:`GenerationInProgressError`). The lock serialises concurrent
   enqueue requests for the same session so the second one always sees
   the first's ``running`` status.
2. In one transaction:
   - DELETE the per-session output stream so the next turn's events
     don't tail onto stale history;
   - Lazy-sweep this user's open credit reservations into terminal
      state (settles from durable ``session_turn`` usage, or refunds
      if usage is missing);
   - INSERT the ``session_turn`` row (status=``pending``) — its
     auto-generated id is the ``turn_id`` carried on the reservation;
   - Reserve credits for this turn (debits ``user.credit_balance``,
     inserts ``credit_ledger`` RESERVE row + ``credit_reservation``);
     raises :class:`~app.errors.InsufficientCreditsError` if the
     estimate would push the balance negative — the entire tx rolls
     back, no message stored;
   - INSERT the user ``message`` row;
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
from app.models.session_files import SessionFile
from app.models.session_turn import SessionTurn, SessionTurnStatus
from app.schemas.session import FileRef
from app.services import credits
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
    2. Lazy-sweep this user's open credit reservations into terminal
       state. Settle reads durable ``session_turn.input_tokens`` /
       ``output_tokens`` / ``terminated_at`` written by the worker,
       not the Redis stream. Skipped on legacy sessions without a
       ``user_id``.
    3. DELETE the previous turn's output stream so new events don't
       tail onto stale history.
    4. INSERT the ``session_turn`` (status=``pending``) and flush so
       its auto-generated id is the ``turn_id`` carried on the new
       reservation.
    5. Reserve credits for this turn — raises
       :class:`~app.errors.InsufficientCreditsError` (→ HTTP 402) if
       the estimate would push the balance negative. The entire tx
       rolls back; no message stored, no worker wake.
    6. INSERT the user ``message`` row, bind uploaded files to the
       session, flip ``session.status='RUNNING'`` — one commit so a
       refetch between steps can't see a half-applied state.
    7. ``publish_wake`` so an idle worker doesn't have to wait for
       its poll cycle. **After commit** so the worker's claim sees
       the row. Best-effort; poll fallback covers a publish failure.

    Sandbox ownership lives on ``sandbox.session_id``. The worker
    resumes the latest non-TERMINATED sandbox for this session, or
    creates a fresh one when none exists.

    Raises:
        GenerationInProgressError: a turn is already streaming.
        InsufficientCreditsError: user's balance is below the
            estimated max cost.
    """
    session = await _lock_session(db, session.id)
    if session.status == SessionStatus.RUNNING:
        raise GenerationInProgressError(
            "A completion is already in progress for this session"
        )

    # Lazy sweep: settle any of this user's reservations from prior
    # turns whose session is already terminal. Source of truth is
    # ``session_turn.input_tokens`` / ``output_tokens`` (V2 durable
    # storage), so ordering against the Redis stream ``DELETE`` below
    # doesn't matter. Sessions without a ``user_id`` are treated as
    # system / legacy and don't bill; logged at WARNING so the case
    # surfaces if it ever fires in prod (every production path stamps
    # ``user_id`` at session creation).
    if session.user_id is None:
        logger.warning(
            "queue_user_message: session %s has no user_id — skipping credit reserve",
            session.id,
        )
    else:
        await credits.settle_open_for_user(db, session.user_id)
    # Prior stream lingers post-``agent.stopped`` for late SSE replay;
    # clear before the new turn so new events don't tail onto stale
    # history. ``DELETE`` is a no-op on missing keys.
    await redis.delete(stream_key(SESSION_STREAM_PREFIX, session.id))

    file_dicts = [f.model_dump() for f in files]
    # Insert the session_turn first so we have a stable ``turn_id`` to
    # attach to the credit reservation. The flush below populates
    # ``session_turn.id`` without committing.
    session_turn = SessionTurn(
        session_id=session.id,
        query=content,
        files=file_dicts,
        status=SessionTurnStatus.PENDING,
    )
    db.add(session_turn)
    await db.flush()

    # Reserve credits using the model_id stamped on the session.
    # Raises InsufficientCreditsError if the estimate would push the
    # balance negative — the whole transaction rolls back, including
    # the session_turn we just inserted, so no message is stored and
    # the worker never sees the turn.
    if session.user_id is not None:
        await credits.reserve_for_turn(
            db,
            user_id=session.user_id,
            session_id=session.id,
            turn_id=session_turn.id,
            model_id=session.model_id,
        )

    user_msg = Message(
        session_id=session.id,
        role=MessageRole.USER,
        blocks=[{"type": "text", "content": content, "files": file_dicts}],
    )
    session.status = SessionStatus.RUNNING
    db.add(user_msg)
    # Bind every uploaded file to this session in the same transaction
    # as the message that references it. Authoritative for download
    # authorization — the JSONB-blob copy in ``user_msg.blocks`` is
    # presentation-only. Same file referenced by a future message is
    # silently no-op'd via ON CONFLICT in the worker / migration paths;
    # here a duplicate would be a bug worth surfacing, so we don't
    # swallow it.
    for file_ref in files:
        db.add(SessionFile(session_id=session.id, file_key=file_ref.file_key))
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
