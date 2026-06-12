"""Unit tests for backend-side terminal reconciliation.

No DB — a capturing fake records the emitted statements and feeds back
configurable ``rowcount``s so the gating logic (session flips only when
the turn flip touched a row) and the SQL guards (live-lease exclusion,
RUNNING-guarded session flip) are asserted in isolation.
"""

import uuid

from sqlalchemy.dialects import postgresql

from app.services.session.recovery import fail_abandoned_turn


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeDb:
    """Records every executed statement; returns rowcounts in order."""

    def __init__(self, rowcounts: list[int]) -> None:
        self._rowcounts = list(rowcounts)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        rowcount = self._rowcounts.pop(0) if self._rowcounts else 0
        return _FakeResult(rowcount)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def test_flips_session_when_turn_was_flipped() -> None:
    # Turn flip touches a row → session flip runs, returns True.
    db = _FakeDb(rowcounts=[1])
    session_id = uuid.uuid4()

    result = await fail_abandoned_turn(db, session_id=session_id)

    assert result is True
    assert len(db.statements) == 2  # turn UPDATE, then session UPDATE


async def test_skips_session_flip_when_no_turn_flipped() -> None:
    # Turn flip touched nothing (live lease, or no non-terminal turn) →
    # session is left alone and the caller is told not to refund.
    db = _FakeDb(rowcounts=[0])
    session_id = uuid.uuid4()

    result = await fail_abandoned_turn(db, session_id=session_id)

    assert result is False
    assert len(db.statements) == 1  # only the turn UPDATE ran


async def test_turn_update_excludes_live_leases_and_terminal_turns() -> None:
    db = _FakeDb(rowcounts=[1])
    await fail_abandoned_turn(db, session_id=uuid.uuid4())

    turn_sql = _sql(db.statements[0])
    # Only non-terminal turns are eligible.
    assert "status IN ('PENDING', 'RUNNING')" in turn_sql
    # A worker actively heartbeating keeps a fresh lease — exclude it.
    assert "lease_expires_at IS NULL" in turn_sql
    assert "lease_expires_at <" in turn_sql
    # Flips to FAILED and stamps the terminal markers (clears the lease,
    # sets terminated_at).
    assert "status='FAILED'" in turn_sql
    assert "lease_expires_at=NULL" in turn_sql
    assert "terminated_at=now()" in turn_sql


async def test_session_flip_is_running_guarded() -> None:
    db = _FakeDb(rowcounts=[1])
    session_id = uuid.uuid4()
    await fail_abandoned_turn(db, session_id=session_id)

    session_sql = _sql(db.statements[1])
    # Mirror the worker's flip: only a still-RUNNING session is touched, so
    # a racing real terminal write is never clobbered.
    assert "status = 'RUNNING'" in session_sql
    assert "'FAILED'" in session_sql
    assert str(session_id) in session_sql
