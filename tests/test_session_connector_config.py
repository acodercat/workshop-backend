"""Connectors as session configuration (``_owned_mcp_server_ids``).

They used to ride in on every message. A Switch means "from now on", so
they live on the session now — written once, applied to every turn.

Two rules this file pins, because both are load-bearing and neither is
visible from the endpoint that calls it:

- ownership is enforced at *write* time, by rejecting rather than dropping;
- liveness is deliberately *not*, so disconnecting a Connector does not
  silently erase a user's configuration.

No live database, so the query is pinned by its compiled text.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.public.session import _owned_mcp_server_ids
from app.services.session import connectors as session_connectors
from app.models.session import SessionStatus
from app.models.session_mcp_server import SessionMCPServer
from app.schemas.session import (
    AgentType,
    MessageCreate,
    SessionCreate,
    SessionDetailResponse,
    SessionResponse,
    SessionUpdate,
    Tier,
)


def _db_owning(*ids: int):
    """An ``AsyncSession`` double answering one SELECT with those ids."""
    result = MagicMock()
    result.scalars.return_value = list(ids)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestWhatIsAccepted:
    async def test_an_empty_configuration_does_not_query(self):
        db = _db_owning()

        assert await _owned_mcp_server_ids(db, user_id=42, requested=[]) == []

        db.execute.assert_not_awaited()

    async def test_owned_ids_are_kept_in_the_order_given(self):
        db = _db_owning(3, 7)

        assert await _owned_mcp_server_ids(db, 42, [7, 3]) == [7, 3]

    async def test_the_same_connector_twice_is_one_connector(self):
        db = _db_owning(7)

        assert await _owned_mcp_server_ids(db, 42, [7, 7]) == [7]


class TestWhatIsRejected:
    async def test_an_id_the_caller_does_not_own_is_a_400(self):
        # Dropping it instead would leave the user believing a Connector is
        # configured when it never was — and, on a neighbour's id, would park
        # a foreign row in this user's session.
        db = _db_owning(7)

        with pytest.raises(HTTPException) as excinfo:
            await _owned_mcp_server_ids(db, 42, [7, 99])

        assert excinfo.value.status_code == 400
        assert "99" in str(excinfo.value.detail)

    async def test_ownership_is_checked_but_connected_is_not(self):
        # Configuration records intent. A Connector disconnected after it was
        # configured must survive in the row — enqueue re-checks liveness per
        # turn and drops it there, so reconnecting restores it without the
        # user having to re-pick.
        db = _db_owning(7)

        await _owned_mcp_server_ids(db, 42, [7])

        compiled = str(db.execute.await_args.args[0])
        assert "mcp_server.id IN " in compiled
        assert "mcp_server.user_id = " in compiled
        assert "connected" not in compiled


class TestTheWireCarriesItOnTheSession:
    def test_a_message_no_longer_takes_connectors(self):
        # The field is gone, not renamed. Pydantic ignores unknown keys, so an
        # old client still sends and is simply not heard — which is the point
        # of asserting it here rather than trusting the schema to complain.
        assert "connectors" not in MessageCreate.model_fields

    def test_creation_carries_the_initial_set(self):
        assert SessionCreate().connectors == []
        assert SessionCreate(connectors=[3]).connectors == [3]

    def test_renaming_a_session_cannot_touch_them(self):
        # They moved to their own sub-resource. Leaving them on ``PATCH`` too
        # would be two ways to write one thing, and the whole-set shape is the
        # one where two tabs toggling different Connectors overwrite each other.
        assert "connectors" not in SessionUpdate.model_fields

    def test_only_the_detail_response_carries_them(self):
        # The list endpoint returns ``SessionResponse`` per row; reading the
        # configuration for each would be a query per session for something no
        # caller reads there. The detail response is where the composer gets it,
        # and the handler passes it explicitly — nothing else in the suite
        # constructs either, and the first cut of this shipped a detail response
        # that could not be built at all.
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            id=uuid4(), user_id=1, title="t", status=SessionStatus.IDLE,
            tier=list(Tier)[0], agent_type=list(AgentType)[0],
            created_at=now, updated_at=now,
        )

        assert "connectors" not in SessionResponse.model_fields
        SessionResponse.model_validate(row)
        assert (
            SessionDetailResponse(
                id=row.id, title=row.title, status=row.status, tier=row.tier,
                agent_type=row.agent_type, connectors=[3, 7],
                created_at=now, updated_at=now, messages=[],
            ).connectors
            == [3, 7]
        )


class TestWhyItIsATableAndNotAnArray:
    def test_deleting_a_connector_takes_it_out_of_every_session(self):
        # The whole reason for the table. As a JSONB id array on ``session``
        # this could not be expressed, and every deletion path would have had
        # to remember to prune — starting with the one that forgot.
        column = SessionMCPServer.__table__.c.mcp_server_id
        foreign_key = next(iter(column.foreign_keys))

        assert foreign_key.column.table.name == "mcp_server"
        assert foreign_key.ondelete == "CASCADE"

    def test_deleting_a_session_takes_its_configuration_with_it(self):
        column = SessionMCPServer.__table__.c.session_id
        foreign_key = next(iter(column.foreign_keys))

        assert foreign_key.column.table.name == "session"
        assert foreign_key.ondelete == "CASCADE"

    def test_the_same_connector_cannot_be_configured_twice(self):
        # Deduplication happens in ``_owned_mcp_server_ids`` too; this is the
        # backstop that does not depend on remembering to call it.
        assert [c.name for c in SessionMCPServer.__table__.primary_key] == [
            "session_id", "mcp_server_id",
        ]

    def test_the_cascade_from_mcp_server_has_an_index(self):
        # The composite PK leads with ``session_id``, so it cannot serve that
        # cascade; Postgres does not auto-index foreign keys.
        assert any(
            index.name == "ix_session_mcp_server_mcp_server_id"
            for index in SessionMCPServer.__table__.indexes
        )


class TestWritingTheConfiguration:
    async def test_the_delete_runs_before_the_insert(self):
        # Both statements are Core so the order is the code's, not the unit of
        # work's. Adding the rows through the session would let a set that
        # overlaps the old one re-insert keys the delete has not run yet.
        db = MagicMock()
        db.execute = AsyncMock()

        await session_connectors.replace(db, uuid4(), [3, 7])

        statements = [str(call.args[0]) for call in db.execute.await_args_list]
        assert len(statements) == 2
        assert statements[0].startswith("DELETE FROM session_mcp_server")
        assert statements[1].startswith("INSERT INTO session_mcp_server")

    async def test_an_empty_configuration_deletes_and_inserts_nothing(self):
        # ``insert()`` with no rows is an error in SQLAlchemy, not a no-op.
        db = MagicMock()
        db.execute = AsyncMock()

        await session_connectors.replace(db, uuid4(), [])

        assert db.execute.await_count == 1
        assert str(db.execute.await_args.args[0]).startswith("DELETE FROM")

    async def test_reading_is_ordered_so_the_payload_is_stable(self):
        # The table does not keep the order they were picked in, and an
        # arbitrary one would make the detail response diff-noisy for no gain.
        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value = [3, 7]
        db.execute = AsyncMock(return_value=result)

        assert await session_connectors.configured_ids(db, uuid4()) == [3, 7]
        assert "ORDER BY" in str(db.execute.await_args.args[0])


class TestTogglingOne:
    async def test_switching_on_twice_is_not_an_error(self):
        # Idempotent against the composite primary key. A second tab that got
        # there first must not turn this into a 500.
        db = MagicMock()
        db.execute = AsyncMock()

        await session_connectors.add(db, uuid4(), 7)

        statement = str(db.execute.await_args.args[0])
        assert statement.startswith("INSERT INTO session_mcp_server")
        assert "ON CONFLICT" in statement and "DO NOTHING" in statement

    async def test_switching_off_names_both_halves_of_the_key(self):
        # Without ``mcp_server_id`` this would clear the session's whole
        # configuration instead of one Connector.
        db = MagicMock()
        db.execute = AsyncMock()

        await session_connectors.remove(db, uuid4(), 7)

        statement = str(db.execute.await_args.args[0])
        assert statement.startswith("DELETE FROM session_mcp_server")
        assert "session_mcp_server.session_id = " in statement
        assert "session_mcp_server.mcp_server_id = " in statement
