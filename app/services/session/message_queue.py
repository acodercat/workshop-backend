"""The messages waiting in a session's queue, and how one goes out.

A message sent while a turn runs waits as a ``queued_message`` row (see
:mod:`app.models.queued_message`). This module holds the rules about the
queue that are more than a query: which rows are still waiting, what
sending one on means, what steering the running turn with one means, and
when a finished turn is waiting on the user rather than on the queue.
Adding, reordering and deleting are plain row edits and live with the
endpoints.

Not to be confused with :func:`~app.services.session.enqueue.queue_user_message`,
which hands a message to the *worker's* queue as a turn. A queued message
is one step before that: it waits for the session, then takes that same
call.
"""

from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.queued_message import QueuedMessage
from app.models.session import Session, SessionStatus
from app.models.session_turn import SessionTurn, SessionTurnStatus
from app.schemas.session import FileRef
from app.services.session.enqueue import (
    bind_session_files,
    filter_invokable_skills,
    queue_user_message,
    retain_bound_files,
)

# The tool a turn ends on when the agent asks the user something. Its name
# is axon's; nothing else here knows the tool.
_ASK_USER_TOOL = "ask_user"


def waiting() -> ColumnElement[bool]:
    """Whether a queued message is waiting: not taken by a turn that is still
    running. A message a turn took to steer is in flight while that turn
    runs; the turn's terminal write deletes it with the record that holds
    it, and a turn that ended without that write — a sweep failed it —
    leaves it waiting again, with nothing to undo."""
    return ~exists().where(
        SessionTurn.id == QueuedMessage.steered_turn_id,
        SessionTurn.status == SessionTurnStatus.RUNNING,
    )


async def list_queued_messages(db: AsyncSession, session_id: UUID) -> list[QueuedMessage]:
    """The session's waiting messages, in the order they go out."""
    rows = await db.execute(
        select(QueuedMessage)
        .where(QueuedMessage.session_id == session_id, waiting())
        .order_by(QueuedMessage.position, QueuedMessage.id)
    )
    return list(rows.scalars().all())


def queue_is_moving(session: Session, queue: list[QueuedMessage]) -> bool:
    """Whether ``queue`` is about to go out with no one doing anything: its
    session's last turn completed and nothing holds it. The dispatcher may
    still find that turn asked a question, and hold it then.

    The same condition the dispatcher selects sessions by, so a message the
    owner sends meanwhile can join the queue rather than overtake it."""
    return (
        session.status is SessionStatus.COMPLETE
        and bool(queue)
        and all(queued.held_at is None for queued in queue)
    )


async def dispatch_queued_message(
    db: AsyncSession,
    redis: aioredis.Redis,
    session: Session,
    queued: QueuedMessage,
) -> None:
    """Send ``queued`` on as the session's next turn.

    The one way a waiting message goes out, for the background dispatcher
    and the owner's "send now" alike: through ``queue_user_message``, so it
    is refused, billed and run exactly as a message sent by hand at this
    moment would be. Nothing here commits. ``queue_user_message`` does, and
    the row's removal rides that commit — the message leaves the queue iff
    its turn exists. On a refusal the caller rolls back and the row stays.
    """
    await db.delete(queued)
    await queue_user_message(
        db,
        redis,
        session,
        content=queued.content,
        files=[FileRef.model_validate(file) for file in queued.files],
        skills=queued.skills,
        timezone=queued.timezone,
    )


async def mark_for_steering(db: AsyncSession, session: Session, queued: QueuedMessage) -> None:
    """Mark ``queued`` for the running turn to take, before the agent's next
    model call, and commit.

    What ``queue_user_message`` would do for its files and skills is done
    here: a message the worker takes into a running turn passes through no
    such call. Its files are bound to the session, for the worker to stage
    what the row names; its skills are resolved and snapshotted
    (``skill_snapshots``), for the worker to load. Both again without harm
    if the turn ends first and the message goes out as a turn of its own
    after all.
    """
    newly_bound_keys = await bind_session_files(
        db,
        session_id=session.id,
        user_id=session.user_id,
        files=[FileRef.model_validate(file) for file in queued.files],
    )
    queued.skill_snapshots = await filter_invokable_skills(db, session.user_id, queued.skills)
    queued.steer = True
    await db.commit()
    await retain_bound_files(session.id, newly_bound_keys)


def awaits_answer(blocks: list[dict[str, Any]]) -> bool:
    """Whether an assistant message ends its turn on a question to the user.

    True when the message holds an ``ask_user`` call that succeeded: the
    turn stopped there to wait for the reply, and a queued message is not
    that reply. A refused call does not count — the agent retried it or
    moved on. The same reading the frontend makes to draw the question
    panel, from the same blocks, so the two cannot disagree about a turn.
    """
    asked = {
        block["tool_use_id"]
        for block in blocks
        if block.get("type") == "tool_use" and block.get("tool") == _ASK_USER_TOOL
    }
    return any(
        block.get("type") == "tool_result"
        and block.get("tool_use_id") in asked
        and not block.get("is_error")
        for block in blocks
    )
