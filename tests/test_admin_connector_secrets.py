"""The console's Connector surface must not be able to carry a secret.

Connector rows hold sealed credentials (``encrypted_credential``,
``oauth_tokens_encrypted``) and catalog rows hold a sealed OAuth client
secret. The console needs none of them: it publishes catalog entries and
answers "why is this failing".

Rather than trusting each endpoint to remember to omit them, this pins
the property at the schema layer — a field that does not exist cannot be
leaked by a later "just add the column" change. Asserted against the
model's real column list, so a *new* secret column added to the ORM also
fails here until someone decides it is safe.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from app.models.mcp_server import MCPServer
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.schemas.admin.connector import (
    ConnectorCatalogEntryCreate,
    ConnectorCatalogEntryResponse,
    ConnectorCatalogEntryUpdate,
    ConnectorResponse,
)

# Substrings that mark a column as holding (or sealing) a credential.
_SECRET_MARKERS = ("encrypted", "secret", "token", "credential")

_ADMIN_MODELS = (
    ConnectorResponse,
    ConnectorCatalogEntryResponse,
    ConnectorCatalogEntryCreate,
    ConnectorCatalogEntryUpdate,
)


def _secret_columns(model) -> set[str]:
    return {
        column.key
        for column in model.__table__.columns
        if any(marker in column.key.lower() for marker in _SECRET_MARKERS)
    }


def test_the_tables_do_hold_secrets() -> None:
    # Guard the guard: if these ever come back empty the tests below
    # would pass vacuously.
    assert _secret_columns(MCPServer)
    assert _secret_columns(MCPServerCatalogEntry)


def test_no_console_schema_exposes_a_connector_secret() -> None:
    forbidden = _secret_columns(MCPServer) | _secret_columns(MCPServerCatalogEntry)

    for model in _ADMIN_MODELS:
        leaked = forbidden & set(model.model_fields)
        assert not leaked, f"{model.__name__} exposes {leaked}"


def test_no_console_schema_has_a_secret_shaped_field_at_all() -> None:
    # Beyond the current columns: a field merely *named* like a secret
    # has no business on this surface either.
    for model in _ADMIN_MODELS:
        for field in model.model_fields:
            assert not any(marker in field.lower() for marker in _SECRET_MARKERS), (
                f"{model.__name__}.{field} looks like a secret"
            )


def test_the_catalog_reports_client_presence_without_the_client() -> None:
    # An operator needs to know *whether* a preregistered OAuth app
    # exists (it explains why authorization skips dynamic registration),
    # but neither the id nor the secret.
    fields = set(ConnectorCatalogEntryResponse.model_fields)

    assert "has_preregistered_client" in fields
    assert "oauth_client_id" not in fields
