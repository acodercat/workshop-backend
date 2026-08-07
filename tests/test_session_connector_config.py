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
from app.models.session import SessionStatus
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

    def test_a_session_is_created_and_updated_with_them(self):
        assert SessionCreate().connectors == []
        assert SessionCreate(connectors=[3]).connectors == [3]
        # ``None`` and ``[]`` are different answers: leave alone, or turn
        # everything off.
        assert SessionUpdate(title="x").connectors is None
        assert SessionUpdate(connectors=[]).connectors == []

    def test_both_responses_can_actually_be_built(self):
        # Each is built a different way — ``SessionResponse`` by validating the
        # ORM row, the detail one field by field in the handler — and the first
        # cut got the second wrong: a ``validation_alias`` made ``connectors``
        # unreachable by name, so every ``GET /sessions/{id}`` raised. Nothing
        # else in the suite constructs either.
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            id=uuid4(), user_id=1, title="t", status=SessionStatus.IDLE,
            tier=list(Tier)[0], agent_type=list(AgentType)[0],
            mcp_server_ids=[3, 7], created_at=now, updated_at=now,
        )

        assert SessionResponse.model_validate(row).connectors == [3, 7]
        assert (
            SessionDetailResponse(
                id=row.id, title=row.title, status=row.status, tier=row.tier,
                agent_type=row.agent_type, connectors=row.mcp_server_ids,
                created_at=now, updated_at=now, messages=[],
            ).connectors
            == [3, 7]
        )
