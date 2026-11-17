from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.session import SessionStatus
from app.services.credits import settle


class _Result:
    def __init__(self, row) -> None:
        self._row = row

    def all(self):
        return self._row

    def one_or_none(self):
        return self._row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row) -> None:
        self._row = row
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self._row)


async def test_read_usage_returns_tokens_and_turn_seconds() -> None:
    created_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    terminated_at = created_at + timedelta(seconds=42.5)
    db = _Db((123, 456, created_at, terminated_at))

    usage = await settle._read_usage_and_duration_from_session_turn(db, 99)

    assert usage == (123, 456, 42.5)


async def test_read_usage_uses_zero_duration_for_legacy_rows() -> None:
    created_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    db = _Db((123, 456, created_at, None))

    usage = await settle._read_usage_and_duration_from_session_turn(db, 99)

    assert usage == (123, 456, 0.0)


async def test_read_usage_returns_none_when_tokens_are_missing() -> None:
    created_at = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    terminated_at = created_at + timedelta(seconds=42.5)
    db = _Db((None, 456, created_at, terminated_at))

    usage = await settle._read_usage_and_duration_from_session_turn(db, 99)

    assert usage is None


# ── _load_reservation_for_update: double-settle concurrency guard ────────────


async def test_load_for_update_refreshes_stale_identity_map_state() -> None:
    """The FOR UPDATE reservation load must carry ``populate_existing`` so it
    reads the row's *current* state after acquiring the lock — not the stale
    copy ``settle_open_for_session`` pre-loaded into the same session.

    Dropping it silently reintroduces the double-settle race: a concurrent
    settle that committed ``state = SETTLED`` while we blocked on the lock
    would be invisible, the ``state is OPEN`` guard would pass on stale data,
    and the settle rows would double-post. A real two-session race needs a DB
    (integration tier); this pins the load-bearing option so a refactor can't
    remove it by accident."""
    db = _Db(row=None)

    await settle._load_reservation_for_update(db, reservation_id=6069)

    assert db.statement.get_execution_options().get("populate_existing") is True


# ── settle_open_for_session: pre-terminal safety ────────────────────────────


class _RoutingDb:
    """Stand-in DB for the routing test. ``settle_open_for_session``
    calls ``db.execute`` once (the join SELECT); ``_read_usage_...`` would
    call it a second time. We track every execute so the test can
    assert that the second call never happens on the safety branch."""

    def __init__(self, first_row) -> None:
        self._first_row = first_row
        self.execute_calls = 0

    async def execute(self, statement):
        self.execute_calls += 1
        # Only the first call (the join SELECT) is exercised in the
        # safety-branch test. If a second call sneaks in, the test
        # will assert against ``execute_calls`` and surface it.
        return _Result(self._first_row)


@pytest.fixture(autouse=True)
def _spy_settle_and_refund(monkeypatch):
    """Replace the two terminal handlers with AsyncMocks so the routing
    test can verify they were NOT awaited on the non-terminal path,
    without needing a real DB session for ``settle_turn`` /
    ``refund_reservation`` to operate on."""
    settle_spy = AsyncMock()
    refund_spy = AsyncMock()
    monkeypatch.setattr(settle, "settle_turn", settle_spy)
    monkeypatch.setattr(settle, "refund_reservation", refund_spy)
    return settle_spy, refund_spy


async def test_settle_open_for_session_skips_when_session_not_terminal(
    _spy_settle_and_refund,
) -> None:
    """The defining safety check: a premature trigger (worker bug,
    errant manual publish) arrives while the session is still RUNNING.
    ``settle_open_for_session`` must NOT refund — leaving the
    reservation OPEN for the legitimate later trigger.

    Without this guard a stray ``settle:notify`` publish would refund
    the user mid-turn, after which the real terminal write would
    settle into a CANCELLED reservation (no-op) and the platform
    eats the LLM cost silently.
    """
    settle_spy, refund_spy = _spy_settle_and_refund
    open_reservation = MagicMock(id=42, turn_id=99)
    db = _RoutingDb((open_reservation, SessionStatus.RUNNING))

    await settle.settle_open_for_session(db, uuid4())

    settle_spy.assert_not_awaited()
    refund_spy.assert_not_awaited()
    # Exactly one execute fired (the routing SELECT). If a usage
    # lookup had run, count would be 2.
    assert db.execute_calls == 1


async def test_settle_open_for_session_no_op_when_no_reservation(
    _spy_settle_and_refund,
) -> None:
    settle_spy, refund_spy = _spy_settle_and_refund
    db = _RoutingDb(None)

    await settle.settle_open_for_session(db, uuid4())

    settle_spy.assert_not_awaited()
    refund_spy.assert_not_awaited()
    assert db.execute_calls == 1


async def test_settle_open_for_session_skips_when_session_row_missing(
    _spy_settle_and_refund,
) -> None:
    """Outer join surfaces a non-existent session row as ``status=None``.
    Treated as non-terminal → no-op (sweeper's expires_at branch will
    eventually refund this corrupted reservation)."""
    settle_spy, refund_spy = _spy_settle_and_refund
    open_reservation = MagicMock(id=42, turn_id=99)
    db = _RoutingDb((open_reservation, None))

    await settle.settle_open_for_session(db, uuid4())

    settle_spy.assert_not_awaited()
    refund_spy.assert_not_awaited()


async def test_settle_open_for_user_locks_what_it_settles_before_the_user_row() -> None:
    """The sweep locks every reservation it will close in its selecting
    statement, in id order, so it holds them all before any settle takes the
    user row. Locking them one per iteration would ask for the next
    reservation while holding the user row — and deadlock against the
    sweeper or the settle subscriber, which lock a reservation and then the
    user row. Only terminal or hard-deleted sessions' reservations are
    selected, so a running turn's is never held."""
    db = _Db([])

    await settle.settle_open_for_user(db, 7)

    sql = str(
        db.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "ORDER BY credit_reservation.id ASC FOR UPDATE OF credit_reservation" in sql
    assert (
        "credit_reservation.session_id IS NULL "
        "OR session.status IN ('COMPLETE', 'FAILED', 'CANCELLED')"
    ) in sql
