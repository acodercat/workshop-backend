"""Pinning a session: the cap, the list order, and that it is not activity."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.api.public import session as endpoint
from app.errors import SessionPinLimitError
from app.models.session import AgentType, Session, SessionStatus, Tier
from app.schemas.session import SessionUpdate
from app.services.session import pinning


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _session(*, pinned: bool) -> Session:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    return Session(
        id=uuid4(),
        user_id=7,
        title="t",
        status=SessionStatus.IDLE,
        tier=Tier.STANDARD,
        agent_type=AgentType.FULL,
        created_at=now,
        updated_at=now,
        pinned_at=now if pinned else None,
    )


def _db(*, pinned_count: int = 0) -> MagicMock:
    """A session that answers the cap's count with ``pinned_count``."""
    return MagicMock(
        scalar=AsyncMock(return_value=pinned_count), commit=AsyncMock(), refresh=AsyncMock()
    )


class TestSetPinned:
    async def test_pinning_stamps_the_time(self) -> None:
        session = _session(pinned=False)

        await pinning.set_pinned(_db(), session, True)

        assert str(session.pinned_at) == "now()"

    async def test_pinning_a_pinned_session_keeps_its_place(self) -> None:
        session = _session(pinned=True)
        pinned_at = session.pinned_at
        db = _db()

        await pinning.set_pinned(db, session, True)

        assert session.pinned_at == pinned_at
        db.scalar.assert_not_awaited()

    async def test_a_pin_past_the_cap_is_refused(self) -> None:
        session = _session(pinned=False)
        db = _db(pinned_count=pinning.MAX_PINNED_SESSIONS)

        with pytest.raises(SessionPinLimitError):
            await pinning.set_pinned(db, session, True)

        assert session.pinned_at is None
        # The cap counts only the owner's live, pinned sessions.
        sql = _sql(db.scalar.await_args.args[0])
        assert "session.user_id = 7" in sql
        assert "session.deleted_at IS NULL" in sql
        assert "session.pinned_at IS NOT NULL" in sql

    async def test_unpinning_is_never_refused(self) -> None:
        session = _session(pinned=True)
        db = _db(pinned_count=pinning.MAX_PINNED_SESSIONS)

        await pinning.set_pinned(db, session, False)

        assert session.pinned_at is None
        db.scalar.assert_not_awaited()


class TestUpdateSession:
    """``PATCH /sessions/{id}``: pinning is not activity on the session."""

    @staticmethod
    async def _patch(monkeypatch, session: Session, body: SessionUpdate) -> SimpleNamespace:
        """Run the handler; return what it was about to write to the row."""
        written = SimpleNamespace()

        async def refresh(row: Session) -> None:
            # The database resolves the SQL expressions a real refresh
            # reads back; here they are recorded, then replaced.
            written.updated_at, written.pinned_at = row.updated_at, row.pinned_at
            row.updated_at = datetime(2026, 11, 2, tzinfo=UTC)
            if row.pinned_at is not None:
                row.pinned_at = datetime(2026, 11, 2, tzinfo=UTC)

        db = _db()
        db.refresh = refresh
        monkeypatch.setattr(endpoint, "_get_or_404", AsyncMock(return_value=session))
        # Past the rate limiter, which wants a live request.
        await endpoint.update_session.__wrapped__(
            request=None,
            session_id=session.id,
            body=body,
            db=db,
            current_user=SimpleNamespace(id=session.user_id),
        )
        return written

    @pytest.mark.parametrize("pinned", [True, False])
    async def test_only_pinning_keeps_updated_at(self, monkeypatch, pinned: bool) -> None:
        session = _session(pinned=not pinned)

        written = await self._patch(monkeypatch, session, SessionUpdate(pinned=pinned))

        # Set to itself, so ``onupdate`` does not stamp now(): an unpinned
        # session would otherwise jump to the top of the recent ones.
        assert written.updated_at is Session.updated_at

    async def test_a_rename_sent_with_a_pin_still_counts_as_activity(self, monkeypatch) -> None:
        session = _session(pinned=False)

        written = await self._patch(
            monkeypatch, session, SessionUpdate(title="renamed", pinned=True)
        )

        assert session.title == "renamed"
        assert str(written.pinned_at) == "now()"
        assert written.updated_at is not Session.updated_at

    async def test_a_rename_alone_leaves_the_pin_alone(self, monkeypatch) -> None:
        session = _session(pinned=True)
        pinned_at = session.pinned_at

        written = await self._patch(monkeypatch, session, SessionUpdate(title="renamed"))

        assert written.pinned_at == pinned_at
        assert written.updated_at is not Session.updated_at


class _ListDb:
    """Records the statements ``list_sessions`` runs: the count, then the page."""

    def __init__(self) -> None:
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        result.scalar.return_value = 0
        result.scalars.return_value.all.return_value = []
        return result


async def test_the_list_puts_pinned_sessions_first() -> None:
    db = _ListDb()

    await endpoint.list_sessions(
        page=1,
        page_size=20,
        search=None,
        project_id=None,
        db=db,
        current_user=SimpleNamespace(id=7),
    )

    page_sql = _sql(db.statements[-1])
    assert "ORDER BY session.pinned_at DESC NULLS LAST, session.updated_at DESC" in page_sql
