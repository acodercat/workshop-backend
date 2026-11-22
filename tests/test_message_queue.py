"""A session's message queue: the body a queued message takes, how one goes
out, when a finished turn waits on the user instead, and the endpoints'
own rules (only a running session takes one; a reorder is an ordering; only
text steers a running turn).

No DB — fakes stand in for the session, and ``queue_user_message`` is
replaced where the point is what it is handed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.public import session as endpoint
from app.errors import (
    QueuedMessageLimitError,
    QueuedMessageNotFoundError,
    SessionIdleError,
)
from app.models.queued_message import QueuedMessage
from app.models.session import SessionStatus
from app.schemas.session import (
    QUEUED_MESSAGES_MAX,
    QueuedMessageCreate,
    QueuedMessageOrder,
)
from app.services.session import message_queue
from app.services.session.message_queue import (
    awaits_answer,
    dispatch_queued_message,
    queue_is_moving,
)

_FILE = {"file_key": "u/1/a.txt", "filename": "a.txt", "mime_type": None, "size_bytes": 3}


def _queued(queued_id: int, position: int = 0, **overrides) -> QueuedMessage:
    values = {
        "id": queued_id,
        "session_id": uuid4(),
        "position": position,
        "content": f"message {queued_id}",
        "files": [],
        "skills": [],
        "timezone": None,
        "held_at": None,
        "steer_turn_id": None,
        "steer_taken": False,
    }
    return QueuedMessage(**{**values, **overrides})


class TestBody:
    def test_carries_what_a_sent_message_carries(self) -> None:
        body = QueuedMessageCreate(
            content="  hi  ", files=[_FILE], skills=["pdf"], timezone="Asia/Shanghai"
        )

        assert body.content == "hi"
        assert body.files[0].file_key == "u/1/a.txt"
        assert body.skills == ["pdf"]
        assert body.timezone == "Asia/Shanghai"

    def test_an_empty_message_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            QueuedMessageCreate(content="   ")

    def test_attachments_alone_are_a_message(self) -> None:
        assert QueuedMessageCreate(files=[_FILE]).content == ""


class TestDispatch:
    async def test_goes_out_as_the_message_it_was_queued_as(self, monkeypatch) -> None:
        sent = AsyncMock()
        monkeypatch.setattr(message_queue, "queue_user_message", sent)
        db = MagicMock(delete=AsyncMock())
        session = SimpleNamespace(id=uuid4())
        queued = _queued(1, content="hi", files=[_FILE], skills=["pdf"], timezone="Asia/Tokyo")

        await dispatch_queued_message(db, "redis", session, queued)

        db.delete.assert_awaited_once_with(queued)
        (args, kwargs) = sent.await_args
        assert args == (db, "redis", session)
        assert kwargs["content"] == "hi"
        assert [file.file_key for file in kwargs["files"]] == ["u/1/a.txt"]
        assert kwargs["skills"] == ["pdf"]
        assert kwargs["timezone"] == "Asia/Tokyo"

    async def test_leaves_the_commit_to_the_enqueue(self, monkeypatch) -> None:
        # The row's removal and the turn commit together or not at all.
        monkeypatch.setattr(message_queue, "queue_user_message", AsyncMock())
        db = MagicMock(delete=AsyncMock(), commit=AsyncMock())

        await dispatch_queued_message(db, "redis", SimpleNamespace(id=uuid4()), _queued(1))

        db.commit.assert_not_awaited()


def _ask(tool_use_id: str = "call-1") -> dict:
    return {"type": "tool_use", "tool_use_id": tool_use_id, "tool": "ask_user", "args": {}}


def _result(tool_use_id: str = "call-1", **extra) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "output": "", **extra}


class TestAwaitsAnswer:
    def test_a_question_that_stood_is_waited_on(self) -> None:
        assert awaits_answer([{"type": "text", "content": "one thing"}, _ask(), _result()])

    def test_a_refused_question_is_not(self) -> None:
        assert not awaits_answer([_ask(), _result(is_error=True)])

    def test_a_refused_question_asked_again_is(self) -> None:
        blocks = [_ask("a"), _result("a", is_error=True), _ask("b"), _result("b")]

        assert awaits_answer(blocks)

    def test_another_tools_result_is_not_a_question(self) -> None:
        blocks = [
            {"type": "tool_use", "tool_use_id": "call-1", "tool": "bash", "args": {}},
            _result(),
        ]

        assert not awaits_answer(blocks)

    def test_a_plain_reply_is_not(self) -> None:
        assert not awaits_answer([{"type": "text", "content": "done"}])


class TestQueueIsMoving:
    def _session(self, status: SessionStatus) -> SimpleNamespace:
        return SimpleNamespace(status=status)

    def test_a_completed_turn_moves_its_queue(self) -> None:
        assert queue_is_moving(self._session(SessionStatus.COMPLETE), [_queued(1)])

    def test_an_empty_queue_does_not_move(self) -> None:
        assert not queue_is_moving(self._session(SessionStatus.COMPLETE), [])

    @pytest.mark.parametrize("status", [SessionStatus.CANCELLED, SessionStatus.FAILED])
    def test_a_stopped_or_failed_turn_leaves_it_to_the_user(self, status) -> None:
        assert not queue_is_moving(self._session(status), [_queued(1)])

    def test_one_held_message_holds_the_queue(self) -> None:
        queue = [_queued(1), _queued(2, 1, held_at="2026-11-18T08:00:00Z")]

        assert not queue_is_moving(self._session(SessionStatus.COMPLETE), queue)


def _endpoint_db(monkeypatch, *, status: SessionStatus, queue: list[QueuedMessage]) -> MagicMock:
    """A request whose session is the caller's, with ``queue`` waiting."""
    session = SimpleNamespace(id=uuid4(), user_id=7, status=status)
    monkeypatch.setattr(endpoint, "_get_or_404", AsyncMock(return_value=session))
    monkeypatch.setattr(endpoint, "list_queued_messages", AsyncMock(return_value=queue))
    return MagicMock(add=MagicMock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())


def _writes(db: MagicMock) -> list[str]:
    """The order ``db`` was flushed, refreshed and committed in."""
    return [name for name, _, _ in db.mock_calls if name in ("flush", "refresh", "commit")]


async def _create(db: MagicMock) -> QueuedMessage:
    # ``__wrapped__``: the handler beneath the rate limiter, which needs a
    # real request to key on.
    return await endpoint.create_queued_message.__wrapped__(
        request=None,
        session_id=uuid4(),
        body=QueuedMessageCreate(content="next"),
        db=db,
        current_user=SimpleNamespace(id=7),
    )


class TestCreate:
    async def test_joins_the_end_of_the_queue(self, monkeypatch) -> None:
        db = _endpoint_db(monkeypatch, status=SessionStatus.RUNNING, queue=[_queued(1, 4)])

        queued = await _create(db)

        assert queued.position == 5
        assert queued.content == "next"
        # ``steer`` is derived: read before the commit, while the lock holds.
        db.refresh.assert_awaited_once_with(queued, ["steer"])
        assert _writes(db) == ["flush", "refresh", "commit"]

    async def test_an_idle_session_takes_none(self, monkeypatch) -> None:
        # The turn ended first: queued now, the message would wait behind
        # nothing, so the client is told to send it instead.
        db = _endpoint_db(monkeypatch, status=SessionStatus.COMPLETE, queue=[])

        with pytest.raises(SessionIdleError):
            await _create(db)

        db.add.assert_not_called()

    async def test_joins_a_queue_about_to_go_out(self, monkeypatch) -> None:
        # The turn completed and the dispatcher has yet to send the head: a
        # message sent now goes behind it, not ahead.
        db = _endpoint_db(monkeypatch, status=SessionStatus.COMPLETE, queue=[_queued(1, 0)])

        assert (await _create(db)).position == 1

    async def test_a_held_queue_takes_none(self, monkeypatch) -> None:
        held = _queued(1, 0, held_at="2026-11-18T08:00:00Z")
        db = _endpoint_db(monkeypatch, status=SessionStatus.COMPLETE, queue=[held])

        with pytest.raises(SessionIdleError):
            await _create(db)

    async def test_a_full_queue_takes_no_more(self, monkeypatch) -> None:
        queue = [_queued(index, index) for index in range(QUEUED_MESSAGES_MAX)]
        db = _endpoint_db(monkeypatch, status=SessionStatus.RUNNING, queue=queue)

        with pytest.raises(QueuedMessageLimitError) as excinfo:
            await _create(db)

        assert excinfo.value.details == {"limit": QUEUED_MESSAGES_MAX}


class TestReorder:
    async def _reorder(self, monkeypatch, queue: list[QueuedMessage], ids: list[int]) -> list[int]:
        db = _endpoint_db(monkeypatch, status=SessionStatus.RUNNING, queue=queue)
        reordered = await endpoint.reorder_queued_messages(
            session_id=uuid4(),
            body=QueuedMessageOrder(ids=ids),
            db=db,
            current_user=SimpleNamespace(id=7),
        )
        assert [queued.position for queued in reordered] == list(range(len(reordered)))
        return [queued.id for queued in reordered]

    async def test_takes_the_order_given(self, monkeypatch) -> None:
        queue = [_queued(1, 0), _queued(2, 1), _queued(3, 2)]

        assert await self._reorder(monkeypatch, queue, [3, 1, 2]) == [3, 1, 2]

    async def test_a_message_that_went_out_meanwhile_is_ignored(self, monkeypatch) -> None:
        queue = [_queued(2, 1), _queued(3, 2)]

        assert await self._reorder(monkeypatch, queue, [3, 1, 2]) == [3, 2]

    async def test_a_message_the_client_never_saw_keeps_its_place_after(self, monkeypatch) -> None:
        queue = [_queued(1, 0), _queued(2, 1), _queued(3, 2), _queued(4, 3)]

        assert await self._reorder(monkeypatch, queue, [2, 1]) == [2, 1, 3, 4]

    async def test_answers_with_the_queue_read_back_after_the_commit(self, monkeypatch) -> None:
        # A moved row's flush expires its derived ``steer``, which the
        # answer reads: the objects just written cannot be answered as is.
        db = _endpoint_db(monkeypatch, status=SessionStatus.RUNNING, queue=[])
        read_back = [_queued(2, 0), _queued(1, 1)]
        endpoint.list_queued_messages.side_effect = [[_queued(1, 0), _queued(2, 1)], read_back]

        answer = await endpoint.reorder_queued_messages(
            session_id=uuid4(),
            body=QueuedMessageOrder(ids=[2, 1]),
            db=db,
            current_user=SimpleNamespace(id=7),
        )

        assert answer is read_back
        db.commit.assert_awaited_once()


_PDF_SNAPSHOT = {"id": 3, "name": "pdf", "bundle_key": "skills/pdf.zip"}


def test_steer_holds_only_while_the_turn_asked_for_is_open() -> None:
    # Derived, so a turn that ends untaken leaves the message an ordinary
    # one with nothing to clear.
    sql = str(
        select(QueuedMessage).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "session_turn.id = queued_message.steer_turn_id" in sql
    assert "session_turn.status IN ('PENDING', 'RUNNING')" in sql


class TestSteer:
    async def _steer(
        self, monkeypatch, queued: QueuedMessage, open_turn_id: int | None = 31
    ) -> MagicMock:
        db = _endpoint_db(monkeypatch, status=SessionStatus.RUNNING, queue=[])
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: open_turn_id))
        monkeypatch.setattr(endpoint, "_queued_message_or_404", AsyncMock(return_value=queued))
        self.session = endpoint._get_or_404.return_value
        self.bind = AsyncMock(return_value=[_FILE["file_key"]])
        self.retain = AsyncMock()
        self.resolve_skills = AsyncMock(return_value=[_PDF_SNAPSHOT] if queued.skills else [])
        monkeypatch.setattr(message_queue, "bind_session_files", self.bind)
        monkeypatch.setattr(message_queue, "retain_bound_files", self.retain)
        monkeypatch.setattr(message_queue, "filter_invokable_skills", self.resolve_skills)
        # ``__wrapped__``: the handler beneath the rate limiter.
        await endpoint.steer_with_queued_message.__wrapped__(
            request=None,
            session_id=uuid4(),
            queued_message_id=queued.id,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
        return db

    async def test_a_text_message_steers_the_open_turn(self, monkeypatch) -> None:
        queued = _queued(1)

        db = await self._steer(monkeypatch, queued)

        # That turn and no other: once it ends untaken, the message waits.
        assert queued.steer_turn_id == 31
        assert queued.steer_taken is False
        # ``steer`` is derived from the turn: read before the commit, while
        # the lock holds.
        db.refresh.assert_awaited_once_with(queued, ["steer"])
        assert _writes(db) == ["flush", "refresh", "commit"]

    async def test_its_files_are_bound_for_the_worker_to_stage(self, monkeypatch) -> None:
        # No ``queue_user_message`` stands between this row and the turn
        # that takes it, so the binding it would do is done here.
        queued = _queued(1, files=[_FILE])

        db = await self._steer(monkeypatch, queued)

        assert queued.steer_turn_id == 31
        bound = self.bind.await_args.kwargs
        assert [file.file_key for file in bound["files"]] == [_FILE["file_key"]]
        assert (bound["session_id"], bound["user_id"]) == (self.session.id, 7)
        # Kept from the reaper only once the binding stands.
        db.commit.assert_awaited_once()
        self.retain.assert_awaited_once_with(self.session.id, [_FILE["file_key"]])

    async def test_its_skills_are_resolved_for_the_worker_to_load(self, monkeypatch) -> None:
        # Resolved as ``queue_user_message`` would: the same authorization,
        # the same snapshot, so the turn loads the bytes picked now.
        queued = _queued(1, skills=["pdf"])

        await self._steer(monkeypatch, queued)

        assert queued.steer_turn_id == 31
        self.resolve_skills.assert_awaited_once()
        assert self.resolve_skills.await_args.args[1:] == (7, ["pdf"])
        assert queued.skill_snapshots == [_PDF_SNAPSHOT]

    async def test_one_an_earlier_turn_took_is_this_turns_to_take(self, monkeypatch) -> None:
        # That turn ended without recording it — stopped, or its lease lost
        # — so the message waited again, still bearing the take.
        queued = _queued(1, steer_turn_id=30, steer_taken=True)

        await self._steer(monkeypatch, queued)

        assert (queued.steer_turn_id, queued.steer_taken) == (31, False)

    async def test_with_no_turn_open_there_is_nothing_to_steer(self, monkeypatch) -> None:
        queued = _queued(1)

        with pytest.raises(SessionIdleError):
            await self._steer(monkeypatch, queued, open_turn_id=None)

        assert queued.steer_turn_id is None
        self.bind.assert_not_awaited()


class TestDelete:
    async def _delete(self, monkeypatch, *, deleted_id: int | None) -> MagicMock:
        db = _endpoint_db(monkeypatch, status=SessionStatus.RUNNING, queue=[])
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: deleted_id))
        await endpoint.delete_queued_message(
            session_id=uuid4(),
            queued_message_id=1,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
        return db

    async def test_deletes_only_a_message_still_waiting(self, monkeypatch) -> None:
        # The delete states the condition itself: the worker's take holds no
        # session lock, so a check made before the delete could go stale.
        db = await self._delete(monkeypatch, deleted_id=1)

        statement = str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert statement.startswith("DELETE FROM queued_message")
        assert "queued_message.steer_taken" in statement
        assert "session_turn.id = queued_message.steer_turn_id" in statement
        db.commit.assert_awaited_once()

    async def test_one_the_running_turn_took_is_not_found(self, monkeypatch) -> None:
        with pytest.raises(QueuedMessageNotFoundError):
            await self._delete(monkeypatch, deleted_id=None)
