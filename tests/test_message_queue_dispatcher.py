"""The message queue dispatcher: which sessions it selects, and what it does
with the head of one queue — send it, hold the queue, or leave it for the
next tick.

No DB. The candidate query is checked as SQL; ``_dispatch_next`` runs
against a fake session, with ``dispatch_queued_message`` replaced so the
point is the dispatcher's own decisions.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.errors import ConcurrentTaskLimitError, InsufficientCreditsError
from app.workers import message_queue_dispatcher as dispatcher


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class _Rows:
    def all(self) -> list:
        return []


class _CapturingDb:
    statement = None

    async def execute(self, statement) -> _Rows:
        self.statement = statement
        return _Rows()


async def test_selects_completed_sessions_whose_queue_nobody_holds() -> None:
    db = _CapturingDb()

    owner = 7
    await dispatcher._list_candidates(db, skip_sessions=set(), skip_owners={owner}, limit=50)

    sql = _sql(db.statement)
    # Driven from the queue, so only sessions that have one are read.
    assert "session.id IN (SELECT queued_message.session_id" in sql
    # A cancelled or failed turn is not selected: its queue waits for the user.
    assert "session.status = 'COMPLETE'" in sql
    assert "session.deleted_at IS NULL" in sql
    # Reached without authentication, so a suspended owner is filtered here.
    assert "\"user\".status = 'ACTIVE'" in sql
    assert "queued_message.held_at IS NOT NULL" in sql
    # What this tick is already done with is left out of the scan itself.
    assert "session.user_id NOT IN (7)" in sql
    assert sql.index("WHERE") < sql.index("LIMIT")


class _FakeDb:
    """Answers the locked re-read with ``session`` and records the rest."""

    def __init__(self, session) -> None:
        self._session = session
        self.commits = 0
        self.rollbacks = 0
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self._session)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeSession:
    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc) -> bool:
        return False


def _patch(monkeypatch, *, queue_length: int = 2, awaits_answer: bool = False, dispatch=None):
    """Wire ``_dispatch_next`` to a completed session with a queue."""
    session = SimpleNamespace(id=uuid4())
    db = _FakeDb(session)
    queue = [SimpleNamespace(id=index + 1) for index in range(queue_length)]
    dispatch = dispatch or AsyncMock()
    monkeypatch.setattr(dispatcher, "AsyncSessionLocal", lambda: _FakeSession(db))
    monkeypatch.setattr(dispatcher, "list_queued_messages", AsyncMock(return_value=queue))
    monkeypatch.setattr(dispatcher, "_turn_awaits_answer", AsyncMock(return_value=awaits_answer))
    monkeypatch.setattr(dispatcher, "dispatch_queued_message", dispatch)
    return SimpleNamespace(db=db, queue=queue, session=session, dispatch=dispatch)


def _held(db: _FakeDb) -> list[str]:
    return [_sql(statement) for statement in db.statements if "held_at" in _sql(statement)]


async def test_sends_the_head_of_the_queue(monkeypatch) -> None:
    wired = _patch(monkeypatch)

    assert await dispatcher._dispatch_next(wired.session.id, "redis")

    wired.dispatch.assert_awaited_once_with(wired.db, "redis", wired.session, wired.queue[0])
    assert not _held(wired.db)


async def test_an_emptied_queue_sends_nothing(monkeypatch) -> None:
    # The owner deleted the last message between the scan and the lock.
    wired = _patch(monkeypatch, queue_length=0)

    assert not await dispatcher._dispatch_next(wired.session.id, "redis")

    wired.dispatch.assert_not_awaited()


async def test_a_turn_that_asked_a_question_holds_the_queue(monkeypatch) -> None:
    wired = _patch(monkeypatch, awaits_answer=True)

    assert not await dispatcher._dispatch_next(wired.session.id, "redis")

    wired.dispatch.assert_not_awaited()
    assert "WHERE queued_message.id = 1" in _held(wired.db)[0]
    assert wired.db.commits == 1


async def test_a_refusal_holds_the_queue(monkeypatch) -> None:
    refusal = InsufficientCreditsError("not enough")
    wired = _patch(monkeypatch, dispatch=AsyncMock(side_effect=refusal))

    assert not await dispatcher._dispatch_next(wired.session.id, "redis")

    # Rolled back first — the failed attempt took the row out — then held.
    assert wired.db.rollbacks == 1
    assert "WHERE queued_message.id = 1" in _held(wired.db)[0]
    assert wired.db.commits == 1


async def test_a_failed_send_holds_the_queue(monkeypatch) -> None:
    # Not a refusal but a crash: retrying every tick would fail the same way.
    wired = _patch(monkeypatch, dispatch=AsyncMock(side_effect=RuntimeError("boom")))

    assert not await dispatcher._dispatch_next(wired.session.id, "redis")

    assert wired.db.rollbacks == 1
    assert "WHERE queued_message.id = 1" in _held(wired.db)[0]


async def test_the_running_task_cap_is_tried_again_not_held(monkeypatch) -> None:
    wired = _patch(monkeypatch, dispatch=AsyncMock(side_effect=ConcurrentTaskLimitError(3)))

    with pytest.raises(ConcurrentTaskLimitError):
        await dispatcher._dispatch_next(wired.session.id, "redis")

    assert not _held(wired.db)
    assert wired.db.commits == 0


class _LockDb:
    """Grants the tick's advisory lock."""

    async def execute(self, _statement, _params=None):
        return SimpleNamespace(scalar=lambda: True)


def _patch_tick(monkeypatch, candidates: list[tuple], congested_owners: set[int]) -> list:
    """Run ``_tick`` over ``candidates`` (oldest first), with every session
    of ``congested_owners`` at the running-task cap. Returns the sessions
    attempted, in order."""
    attempts: list = []

    async def list_candidates(_db, *, skip_sessions, skip_owners, limit):
        return [
            (session_id, owner)
            for session_id, owner in candidates
            if session_id not in skip_sessions and owner not in skip_owners
        ][:limit]

    async def dispatch_next(session_id, _redis):
        attempts.append(session_id)
        if dict(candidates)[session_id] in congested_owners:
            raise ConcurrentTaskLimitError(3)
        return True

    monkeypatch.setattr(dispatcher, "AsyncSessionLocal", lambda: _FakeSession(_LockDb()))
    monkeypatch.setattr(dispatcher, "_list_candidates", list_candidates)
    monkeypatch.setattr(dispatcher, "_dispatch_next", dispatch_next)
    return attempts


async def test_a_congested_owners_other_sessions_wait_for_the_next_tick(monkeypatch) -> None:
    # The cap is per owner: once one session meets it, the owner's others
    # would too, and each attempt costs a whole enqueue.
    congested, other, free = uuid4(), uuid4(), uuid4()
    attempts = _patch_tick(
        monkeypatch, [(congested, 1), (other, 1), (free, 2)], congested_owners={1}
    )

    assert await dispatcher._tick("redis") == 1
    assert attempts == [congested, free]


async def test_a_congested_owner_cannot_fill_the_batch(monkeypatch) -> None:
    # A whole batch of the oldest candidates belongs to an owner at the cap;
    # the session behind them, whose owner has room, still goes out.
    crowd = [(uuid4(), 1) for _ in range(dispatcher._DISPATCH_BATCH)]
    behind = uuid4()
    attempts = _patch_tick(monkeypatch, [*crowd, (behind, 2)], congested_owners={1})

    assert await dispatcher._tick("redis") == 1
    assert attempts == [crowd[0][0], behind]
