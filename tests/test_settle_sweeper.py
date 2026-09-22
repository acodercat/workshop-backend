from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.models.session import SessionStatus
from app.workers import settle_sweeper


class _Result:
    def all(self) -> list:
        return []


class _CapturingDb:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


async def test_list_candidates_filters_before_limit() -> None:
    db = _CapturingDb()

    await settle_sweeper._list_candidates(db)

    assert db.statement is not None
    sql = str(
        db.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "credit_reservation.state = 'OPEN'" in sql
    assert "credit_reservation.session_id IS NULL" in sql
    assert "session.status NOT IN ('IDLE', 'RUNNING')" in sql
    assert "credit_reservation.expires_at < now()" in sql

    where_index = sql.index("WHERE")
    limit_index = sql.index("LIMIT")
    assert where_index < limit_index


# ── _process_one routes on the FRESH status, not the candidate snapshot ──


_PAST = datetime(2000, 1, 1, tzinfo=UTC)  # always < now() → "expired"


class _FakeDb:
    def __init__(self, live_status) -> None:
        self._live_status = live_status

    async def scalar(self, _statement):
        return self._live_status

    async def commit(self) -> None:
        pass


class _FakeSession:
    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc) -> bool:
        return False


def _patch_process_one(monkeypatch, *, live_status, fail_returns=True) -> dict:
    """Stub the DB session + the settle/refund/recovery collaborators so
    ``_process_one``'s routing can be asserted in isolation. Returns a dict
    recording which collaborators were called."""
    calls: dict[str, list] = {"settle": [], "refund": [], "fail": []}
    db = _FakeDb(live_status)
    monkeypatch.setattr(settle_sweeper, "AsyncSessionLocal", lambda: _FakeSession(db))

    async def fake_settle(_db, session_id):
        calls["settle"].append(session_id)

    async def fake_refund(_db, *, reservation_id, reason):
        calls["refund"].append(reservation_id)

    async def fake_fail(_db, *, session_id):
        calls["fail"].append(session_id)
        return fail_returns

    monkeypatch.setattr(settle_sweeper.credits, "settle_open_for_session", fake_settle)
    monkeypatch.setattr(settle_sweeper.credits, "refund_reservation", fake_refund)
    monkeypatch.setattr(settle_sweeper, "fail_abandoned_turn", fake_fail)
    return calls


async def test_process_one_settles_when_live_status_terminal(monkeypatch) -> None:
    # The M3 race: candidate was listed as expired+non-terminal (stale
    # snapshot), but the worker has since committed COMPLETE + usage. The
    # live re-read must route to settle — never refund the finished turn.
    session_id = uuid4()
    calls = _patch_process_one(monkeypatch, live_status=SessionStatus.COMPLETE)

    touched = await settle_sweeper._process_one((1, session_id, _PAST))

    assert touched is True
    assert calls["settle"] == [session_id]
    assert calls["refund"] == []


async def test_process_one_refunds_expired_dead_worker(monkeypatch) -> None:
    # Live status still RUNNING + expired + no live lease → terminalize and
    # refund.
    session_id = uuid4()
    calls = _patch_process_one(
        monkeypatch,
        live_status=SessionStatus.RUNNING,
        fail_returns=True,
    )

    touched = await settle_sweeper._process_one((2, session_id, _PAST))

    assert touched is True
    assert calls["fail"] == [session_id]
    assert calls["refund"] == [2]
    assert calls["settle"] == []


async def test_process_one_leaves_open_when_worker_still_alive(monkeypatch) -> None:
    # Live status RUNNING + expired but a worker still holds a live lease
    # (fail_abandoned_turn → False) → no refund, left OPEN.
    session_id = uuid4()
    calls = _patch_process_one(
        monkeypatch,
        live_status=SessionStatus.RUNNING,
        fail_returns=False,
    )

    touched = await settle_sweeper._process_one((3, session_id, _PAST))

    assert touched is False
    assert calls["refund"] == []
    assert calls["settle"] == []
