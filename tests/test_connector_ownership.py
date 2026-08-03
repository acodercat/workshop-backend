"""The cross-user gate on every per-connector route.

``_get_owned_connector`` is the only thing standing between a user and another
user's Connector — every ``/connectors/{id}`` route funnels through it — and
every test that touched it stubbed it out, so nothing asserted what it does.

No live database here (the suite runs none, and ``mcp_server`` needs JSONB and a
Postgres enum), so what these pin is the pair of facts the isolation rests on:
the query constrains **both** id and owner, and a query matching nothing is a
404 rather than a leak or a 500. Together those are "user B asking for user A's
connector is told it does not exist" — the third fact, that Postgres honours a
WHERE clause, is not this suite's to prove.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.public import mcp_server as endpoint


def _db_returning(connector):
    """An ``AsyncSession`` double that answers one SELECT, recording it."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = connector
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestTheOwnershipGate:
    async def test_it_constrains_the_owner_and_not_only_the_id(self):
        # The whole gate. Dropping ``user_id`` here — an inviting simplification,
        # since ``id`` is already unique — turns every per-connector route into
        # an IDOR: the id is a small integer, so guessing a neighbour's is
        # counting.
        db = _db_returning(MagicMock())

        await endpoint._get_owned_connector(db, connector_id=7, user_id=42)

        compiled = str(db.execute.await_args.args[0])
        assert "mcp_server.id = " in compiled
        assert "mcp_server.user_id = " in compiled

    async def test_someone_elses_connector_is_not_found(self):
        # 404, not 403: telling the caller a connector exists but is not theirs
        # confirms the id, which is the one bit an enumeration wants.
        db = _db_returning(None)

        with pytest.raises(HTTPException) as caught:
            await endpoint._get_owned_connector(db, connector_id=7, user_id=42)

        assert caught.value.status_code == 404

    async def test_every_per_connector_route_goes_through_it(self):
        # The gate only holds if nothing routes around it. Checked against the
        # source because there is no other way to ask "did a new handler forget":
        # a route that queries MCPServer by id on its own would pass every test
        # in this file and still be an IDOR.
        import inspect

        source = inspect.getsource(endpoint)
        by_id = source.count("MCPServer.id == ")
        assert by_id == 1, (
            "a per-connector query outside _get_owned_connector — if it is a "
            "route handler, it is an IDOR; if it is deliberate, name it here"
        )
