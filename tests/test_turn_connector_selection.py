"""Which Connectors a turn carries (``enqueue._resolve_mcp_servers``).

The set comes from ``session.mcp_server_ids`` — configuration, validated
for ownership when written. This resolves it against the live rows at
enqueue and snapshots the survivors onto the turn.

The first test is still the one that matters: an empty configuration
attaches nothing. A regression to the old "connected = active" behaviour
costs every turn the tokens of every Connector the user holds, on every
step, whether or not it is called — and nothing else in the suite would
notice.

No live database (the suite runs none, and ``mcp_server`` needs JSONB and
a Postgres enum), so the query is pinned by its compiled text.
"""

from unittest.mock import AsyncMock, MagicMock

from app.services.session.enqueue import _resolve_mcp_servers


def _db_returning(*rows: tuple[int, str]):
    """An ``AsyncSession`` double answering one SELECT with ``(id, name)`` rows."""
    result = MagicMock()
    result.all.return_value = list(rows)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestNothingIsAttachedByDefault:
    async def test_an_empty_pick_attaches_nothing(self):
        db = _db_returning((1, "Notion"))

        assert await _resolve_mcp_servers(db, user_id=42, requested=[]) == []

    async def test_an_empty_pick_does_not_even_query(self):
        # Not just an empty result — the turn asks nothing of the table.
        db = _db_returning()

        await _resolve_mcp_servers(db, user_id=42, requested=[])

        db.execute.assert_not_awaited()


class TestWhatAPickIsFilteredBy:
    async def test_the_query_constrains_id_owner_and_connected(self):
        # Dropping any one of the three is a hole: without ``user_id`` a pick
        # reaches a neighbour's Connector (ids are small integers), and without
        # ``connected`` it reaches one the user has disconnected or never
        # authorized, which the worker would then try to stand up.
        db = _db_returning((7, "Notion"))

        await _resolve_mcp_servers(db, user_id=42, requested=[7])

        compiled = str(db.execute.await_args.args[0])
        assert "mcp_server.id IN " in compiled
        assert "mcp_server.user_id = " in compiled
        assert "mcp_server.connected IS " in compiled

    async def test_an_id_the_query_rejects_is_dropped_not_raised(self):
        # Configured, then deleted or disconnected: it costs the turn that one
        # Connector. Failing the send instead would throw away a typed message
        # over a row the user can no longer see.
        db = _db_returning((7, "Notion"))

        picked = await _resolve_mcp_servers(db, user_id=42, requested=[7, 99])

        assert picked == [{"id": 7, "name": "Notion"}]


class TestTheSnapshotShape:
    async def test_it_carries_id_and_name_only(self):
        # Credentials and url are deliberately absent: the worker re-reads the
        # row by id at connect time, so a rotation is picked up and no secret
        # sits on a queued ``session_turn``.
        db = _db_returning((7, "Notion"))

        picked = await _resolve_mcp_servers(db, user_id=42, requested=[7])

        assert picked == [{"id": 7, "name": "Notion"}]
