"""A Connector whose credential no longer decrypts stays manageable.

Rotating ``mcp_credential_encryption_key`` leaves rows sealed under the old
key. Such a row cannot be probed, but a PATCH that probes nothing — a rename,
a disconnect, a tool toggle — never needs the plaintext and must not fail on it.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from app.errors import ConnectorCredentialUnreadableError
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.schemas.mcp_server import ConnectorUpdate

_NOW = datetime(2026, 8, 14, tzinfo=UTC)


def _sealed_under_an_old_key() -> MCPServer:
    return MCPServer(
        id=1,
        user_id=7,
        name="c",
        url="https://mcp.example.com/mcp",
        auth_type=MCPServerAuthType.BEARER,
        encrypted_credential="sealed-under-the-old-key",
        disabled_tools=[],
        advertised_icons=[],
        tools_preview=[],
        tool_count=0,
        resources_enabled=False,
        connected=True,
        oauth_reauthorization_required=False,
        last_connect_failed=False,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _unreadable(sealed, **_):
    raise ConnectorCredentialUnreadableError("sealed under a key this process lacks")


async def _patch(monkeypatch, connector: MCPServer, body: ConnectorUpdate) -> None:
    from app.api.public import mcp_server as endpoint
    from app.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(endpoint, "_get_owned_connector", AsyncMock(return_value=connector))
    monkeypatch.setattr(endpoint, "_reject_duplicate_url", AsyncMock())
    monkeypatch.setattr(endpoint, "release_connection", AsyncMock())
    monkeypatch.setattr(endpoint, "_commit_or_conflict", AsyncMock())
    monkeypatch.setattr(endpoint, "open_secret", _unreadable)
    db = AsyncMock()
    db.new, db.dirty, db.deleted = set(), set(), set()

    await endpoint.update_connector(
        request=MagicMock(),
        connector_id=1,
        body=body,
        background_tasks=BackgroundTasks(),
        db=db,
        redis=AsyncMock(),
        current_user=MagicMock(id=7),
    )


@pytest.mark.parametrize(
    "body",
    [
        ConnectorUpdate(connected=False),
        ConnectorUpdate(name="renamed"),
        ConnectorUpdate(disabled_tools=["search"]),
    ],
)
async def test_a_patch_that_probes_nothing_does_not_decrypt(monkeypatch, body):
    connector = _sealed_under_an_old_key()

    await _patch(monkeypatch, connector, body)


async def test_repointing_still_needs_the_credential(monkeypatch):
    # A URL change probes the new server with the stored credential, so an
    # unreadable one is still refused — there is nothing honest to probe with.
    with pytest.raises(ConnectorCredentialUnreadableError):
        await _patch(
            monkeypatch,
            _sealed_under_an_old_key(),
            ConnectorUpdate(url="https://other.example.com/mcp"),
        )
