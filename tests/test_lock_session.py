"""Unit tests for the enqueue row-lock + delete-race guard.

No DB — a capturing fake feeds back the scalar result and records the
emitted statement, so the ``SessionNotFoundError`` path and the
``FOR UPDATE`` + ``deleted_at IS NULL`` predicate are asserted in
isolation.
"""

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.errors import SessionNotFoundError
from app.models.session import Session, SessionStatus
from app.services.session.enqueue import _lock_session


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    def __init__(self, value) -> None:
        self._value = value
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self._value)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def test_returns_locked_live_session() -> None:
    session = Session(id=uuid4(), status=SessionStatus.IDLE)
    db = _Db(session)

    result = await _lock_session(db, session.id)

    assert result is session


async def test_raises_when_session_gone() -> None:
    # A delete committed between the endpoint's _get_or_404 and this lock:
    # the `deleted_at IS NULL` predicate now matches no row.
    db = _Db(None)

    with pytest.raises(SessionNotFoundError):
        await _lock_session(db, uuid4())


async def test_locks_only_live_rows() -> None:
    db = _Db(Session(id=uuid4(), status=SessionStatus.IDLE))

    await _lock_session(db, db._value.id)

    sql = _sql(db.statement)
    assert "deleted_at IS NULL" in sql
    assert "FOR UPDATE" in sql
