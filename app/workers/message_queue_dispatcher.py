"""Send queued messages on as their sessions' turns complete.

A message sent while a turn runs waits in ``queued_message``. This task is
what sends it on: when a session's turn has completed and its queue is not
empty, the head goes out through ``dispatch_queued_message`` — the same call
the owner's "send now" makes, and under it the same ``queue_user_message``
a message typed into an idle session takes. It runs in the background
process rather than in a browser, so a queue drains whether or not anyone
has the session open.

**What a finished turn does to a queue.** Only a turn that ``COMPLETE``d
carries it on. A cancelled or failed one leaves the session in a status
this task does not select, so the queue stays for the user to decide — and
moves again once a turn they start completes. Three more cases hold a queue
although the turn completed, by stamping ``held_at``:

- the owner stopped the turn as its answer finished: the cancel endpoint
  holds the queue itself (``hold_queue``), so the stop holds however the
  turn ends;
- the turn ended on a question to the user (``awaits_answer``): the queued
  message is not the answer;
- the send failed — ``queue_user_message`` refused it (out of credits, a
  file gone), or it raised. Retrying every tick would fail the same way
  until something changes, and the user is the one who can change it.

A held queue moves again once the session's next turn starts, which clears
every ``held_at`` (``queue_user_message`` does it): the user answering the
question, sending a message by hand, or pressing "send now". Topping up
credits is not a turn, so after one the queue waits for that press. The
client reads ``held_at`` to tell a held queue from a moving one.

The running-task cap is the exception, as it is for a scheduled task: that
is congestion, a slot frees on its own, so nothing is held and the message
is tried again next tick. A tick takes one session per owner, the cap
being per owner: a congested owner costs one attempt a tick. And it takes
every owner whose queue moves, so none waits behind owners at their cap.

**A poll, woken early.** The condition is a state, not an event: a
completed session with a queue nobody holds. The tick reads it every
``_TICK_SECONDS``, which needs no delivery guarantee. So that a queue does
not wait out that gap, the worker's announcement of a terminal turn
(``settle:notify``) starts the next tick at once. The announcement carries
no work of its own — it only ends the wait — so one that is lost costs the
rest of the gap and nothing else, and every rule below has the one path.
The client covers the gap that remains by queueing what the user sends
meanwhile behind the queue (``queue_is_moving``). The scan is driven from
``queued_message``, so it reads only sessions that have a queue.

Concurrency follows the other standing tasks: every host runs this, a
transaction-scoped advisory lock lets one of them scan per tick, and the
session row lock makes each dispatch safe against the owner's own requests.
"""

import asyncio
import contextlib
import logging
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.errors import AppError, ConcurrentTaskLimitError
from app.models.message import Message, MessageRole
from app.models.queued_message import QueuedMessage
from app.models.session import Session, SessionStatus
from app.models.user import User, UserStatus
from app.services.redis import SETTLE_NOTIFY_CHANNEL
from app.services.session.message_queue import (
    awaits_answer,
    dispatch_queued_message,
    list_queued_messages,
)
from app.workers.pubsub import subscribe_forever

logger = logging.getLogger(__name__)

# 64-bit key for the Postgres advisory lock: the bytes ``b"MSGQUEUE"``.
_LOCK_KEY = 0x4D53475155455545

# The longest a completed turn's queue waits before its head goes out, when
# the worker's announcement of that turn was lost. A constant, not a
# setting: it is the gap a user watches between two of their own messages,
# and the scan it buys is a handful of rows.
_TICK_SECONDS = 2


async def run_forever(redis: aioredis.Redis) -> None:
    """Dispatch every tick, and at once when a turn ends. Cancel-safe via
    :class:`asyncio.CancelledError`."""
    turn_ended = asyncio.Event()

    async def note_turn_ended(_session_id: str) -> None:
        turn_ended.set()

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(subscribe_forever(redis, SETTLE_NOTIFY_CHANNEL, note_turn_ended))
        tasks.create_task(_dispatch_forever(redis, turn_ended))


async def _dispatch_forever(redis: aioredis.Redis, turn_ended: asyncio.Event) -> None:
    """Tick, then wait out the gap or until ``turn_ended`` is set."""
    consecutive_failures = 0
    while True:
        # Cleared before the scan: a turn that ends during it is scanned for
        # again rather than left to the gap.
        turn_ended.clear()
        try:
            dispatched = await _tick(redis)
            consecutive_failures = 0
            if dispatched:
                logger.info("message queue tick: dispatched=%d", dispatched)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "message queue tick failed (consecutive_failures=%d)",
                consecutive_failures,
            )
        # Exponential backoff, capped at a minute: a queue is something a
        # user is waiting on, so it must not stay behind a recovered
        # database for long.
        delay = (
            min(_TICK_SECONDS * (2**consecutive_failures), 60)
            if consecutive_failures
            else _TICK_SECONDS
        )
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay):
                await turn_ended.wait()


async def _tick(redis: aioredis.Redis) -> int:
    """One dispatch cycle. Returns how many messages went out (0 when
    another host held the advisory lock).

    Two session lifetimes, as in the settle sweeper: ``lock_db`` holds the
    transaction-scoped advisory lock for the tick and only reads, while each
    session's queue gets its own :class:`AsyncSession`, so a failure on one
    does not poison the rest of the tick.
    """
    async with AsyncSessionLocal() as lock_db:
        got = (
            await lock_db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
        ).scalar()
        if not got:
            return 0
        dispatched = 0
        for session_id in await _list_candidates(lock_db):
            try:
                if await _dispatch_next(session_id, redis):
                    dispatched += 1
            except ConcurrentTaskLimitError:
                pass  # congestion: a slot frees on its own, and the next tick tries again
            except Exception:
                logger.exception("message queue: session %s failed — next tick retries", session_id)
        return dispatched


async def _list_candidates(db: AsyncSession) -> list[UUID]:
    """The sessions whose queue moves — the last turn completed and nothing
    holds the queue, ``queue_is_moving`` in SQL — one per owner, the one
    longest idle, and those longest idle first.

    The owner's account has to still be live. Like the schedule dispatcher,
    this reaches ``queue_user_message`` without passing authentication, the
    one other place ``user.status`` is enforced.
    """
    held = exists().where(
        QueuedMessage.session_id == Session.id, QueuedMessage.held_at.is_not(None)
    )
    each_owners_oldest = (
        select(Session.id, Session.updated_at)
        .join(User, User.id == Session.user_id)
        .where(
            Session.id.in_(select(QueuedMessage.session_id)),
            Session.status == SessionStatus.COMPLETE,
            Session.deleted_at.is_(None),
            User.status == UserStatus.ACTIVE,
            ~held,
        )
        .distinct(Session.user_id)
        .order_by(Session.user_id, Session.updated_at)
        .subquery()
    )
    rows = await db.execute(
        select(each_owners_oldest.c.id).order_by(each_owners_oldest.c.updated_at)
    )
    return list(rows.scalars().all())


async def _dispatch_next(session_id: UUID, redis: aioredis.Redis) -> bool:
    """Send the head of one session's queue, or hold the queue. Returns
    whether a message went out. Raises :class:`ConcurrentTaskLimitError`,
    with nothing changed, when the owner is at the running-task cap."""
    async with AsyncSessionLocal() as db:
        # Re-read under the row lock: the candidate snapshot is stale, and
        # the owner may have sent, stopped or deleted since.
        session = (
            await db.execute(
                select(Session)
                .where(
                    Session.id == session_id,
                    Session.status == SessionStatus.COMPLETE,
                    Session.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return False
        queue = await list_queued_messages(db, session_id)
        if not queue:
            return False
        head = queue[0]
        # Read now: a failed attempt is rolled back, which expires the row.
        head_id = head.id

        if await _turn_awaits_answer(db, session_id):
            await _hold(db, head_id)
            return False
        try:
            await dispatch_queued_message(db, redis, session, head)
        except ConcurrentTaskLimitError:
            raise
        except Exception as failure:
            await db.rollback()
            await _hold(db, head_id)
            # A refusal is an ordinary answer; anything else is a fault to see.
            if isinstance(failure, AppError):
                logger.info("message queue: session %s held — %s", session_id, failure.error_type)
            else:
                logger.exception("message queue: session %s held — send failed", session_id)
            return False
        return True


async def _turn_awaits_answer(db: AsyncSession, session_id: UUID) -> bool:
    """Whether the session's last turn ended on a question to the user."""
    last_message = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (
        last_message is not None
        and last_message.role is MessageRole.ASSISTANT
        and awaits_answer(last_message.blocks)
    )


async def _hold(db: AsyncSession, queued_message_id: int) -> None:
    """Hold the queue at this message until the session's next turn starts."""
    await db.execute(
        update(QueuedMessage)
        .where(QueuedMessage.id == queued_message_id)
        .values(held_at=func.now())
    )
    await db.commit()
