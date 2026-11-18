"""The messages waiting in a session's queue, and how one goes out.

A message sent while a turn runs waits as a ``queued_message`` row (see
:mod:`app.models.queued_message`). This module holds the two rules about
the queue that are more than a query: what sending one on means, and when
a finished turn is waiting on the user rather than on the queue. Adding,
rewording, reordering and deleting are plain row edits and live with the
endpoints.

Not to be confused with :func:`~app.services.session.enqueue.queue_user_message`,
which hands a message to the *worker's* queue as a turn. A queued message
is one step before that: it waits for the session, then takes that same
call.
"""

from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.queued_message import QueuedMessage
from app.models.session import Session, SessionStatus
from app.schemas.session import FileRef
from app.services.session.enqueue import queue_user_message

# The tool a turn ends on when the agent asks the user something. Its name
# is axon's; nothing else here knows the tool.
_ASK_USER_TOOL = "ask_user"


async def list_queued_messages(db: AsyncSession, session_id: UUID) -> list[QueuedMessage]:
    """The session's waiting messages, in the order they go out."""
    rows = await db.execute(
        select(QueuedMessage)
        .where(QueuedMessage.session_id == session_id)
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
