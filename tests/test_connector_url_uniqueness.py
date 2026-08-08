"""One Connector per server, per user — and names free to repeat.

The constraint used to be on the name. It moved because the name is a label
the user chose and the URL is what the Connector *is*: two rows at one URL are
the same server added twice, with the same tools arriving twice in a turn's
catalog and two credentials to keep in step. Two rows sharing a name are not
necessarily anything.

The guard in front of the index is not an optimization. OAuth registration
mints a client at the provider before the row is inserted, so a collision
found by the index leaves that client orphaned — never stored, never
revocable. Asking first is what stops one being minted at all.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.public.mcp_server import _reject_duplicate_url
from app.errors import ConnectorUrlConflictError
from app.models.mcp_server import MCPServer

URL = "https://mcp.notion.com/mcp"


def _db(found):
    """An ``AsyncSession`` double whose SELECT finds *found* (a row or None)."""
    result = MagicMock()
    result.first.return_value = found
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestTheGuard:
    async def test_a_server_the_user_does_not_have_yet_passes(self):
        assert await _reject_duplicate_url(_db(None), 42, URL) is None

    async def test_a_second_connector_for_one_server_is_refused(self):
        with pytest.raises(ConnectorUrlConflictError) as excinfo:
            await _reject_duplicate_url(_db((1,)), 42, URL)

        # The URL, because that is what the user has to change to get past it.
        assert URL in str(excinfo.value)

    async def test_it_asks_about_this_user_only(self):
        # Two users may each hold the same server; the constraint is per-user.
        db = _db(None)

        await _reject_duplicate_url(db, 42, URL)

        compiled = str(db.execute.await_args.args[0])
        assert "mcp_server.user_id = " in compiled
        assert "mcp_server.url = " in compiled

    async def test_the_row_being_edited_does_not_collide_with_itself(self):
        # A PATCH that changes the name while leaving the URL alone would
        # otherwise find its own row and refuse.
        db = _db(None)

        await _reject_duplicate_url(db, 42, URL, excluding=7)

        assert "mcp_server.id != " in str(db.execute.await_args.args[0])

    async def test_without_that_argument_nothing_is_excluded(self):
        # Adding is not editing: there is no row to exempt, and exempting one
        # by accident is how a duplicate gets in.
        db = _db(None)

        await _reject_duplicate_url(db, 42, URL)

        assert "mcp_server.id != " not in str(db.execute.await_args.args[0])


class TestWhatTheTableEnforces:
    def test_one_row_per_user_per_server(self):
        names = {
            constraint.name for constraint in MCPServer.__table__.constraints
        }
        assert "uq_mcp_server_user_id_url" in names

    def test_a_user_may_name_two_connectors_the_same(self):
        # Deliberate, and the reason the old constraint had to go: an official
        # Connector's name is copied from the catalog, and a localized catalog
        # would otherwise make "which language was it added in" decide whether
        # a second, unrelated server could be added at all.
        assert "uq_mcp_server_user_id_name" not in {
            constraint.name for constraint in MCPServer.__table__.constraints
        }
        assert not MCPServer.__table__.c.name.unique
