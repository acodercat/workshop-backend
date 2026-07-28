"""Schemas for the /api/connectors surface (user-created MCP Connectors)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.mcp_server import MCPServerAuthType


def _require_auth_credentials(
    auth_type: MCPServerAuthType, credential: str | None, header_name: str | None,
) -> None:
    """Enforce the credential a given auth type needs.

    BEARER needs a ``credential`` (the token); HEADER needs both ``header_name``
    and ``credential`` (the value); NONE / OAUTH need neither. Shared by the
    create and update validators so the two can't drift.
    """
    if auth_type == MCPServerAuthType.BEARER and not credential:
        raise ValueError("BEARER auth requires a credential (token).")
    if auth_type == MCPServerAuthType.HEADER and not (credential and header_name):
        raise ValueError("HEADER auth requires header_name and credential.")


class ConnectorResponse(BaseModel):
    """A Connector as returned to the client. Never exposes the credential."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    auth_type: str
    tool_count: int
    tools_preview: list[dict[str, str]] | None
    # Whether the agent currently gets this Connector's tools. Disconnecting
    # keeps the credentials, so reconnecting is one click; deleting discards them.
    connected: bool
    # True unless this is an OAuth connector still awaiting authorization.
    # Set by the endpoint layer (the ORM has no such column).
    authorized: bool = True
    # The directory entry this was connected from, else None. The client joins
    # the directory on it to tell an already-connected entry from one on offer.
    catalog_id: int | None = None
    created_at: datetime
    updated_at: datetime


class ConnectorCatalogEntryResponse(BaseModel):
    """One official Connector offered in the directory (a template).

    Operator-side columns (``published``, ``sort_order``) stay server-side: the
    client only ever sees entries it is allowed to add.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    name: str
    url: str
    auth_type: str
    description: str | None
    icon_url: str | None


class _ConnectorAuthFields(BaseModel):
    """Shared auth fields + their cross-field validation.

    BEARER needs a ``credential`` (the token); HEADER needs both
    ``header_name`` and ``credential`` (the value); NONE needs neither.
    """

    auth_type: MCPServerAuthType = MCPServerAuthType.NONE
    credential: str | None = Field(default=None, max_length=8192)
    header_name: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _auth_fields_consistent(self) -> "_ConnectorAuthFields":
        _require_auth_credentials(self.auth_type, self.credential, self.header_name)
        return self


class ConnectorCreate(_ConnectorAuthFields):
    """A Connector is created from exactly one of two sources.

    - **Catalog**: ``catalog_id`` alone — every other field is copied from the
      official entry, so sending one alongside is rejected rather than silently
      ignored (same footgun :class:`ConnectorUpdate` guards).
    - **Custom**: ``name`` + ``url`` (+ the auth fields above).
    """

    catalog_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    # OAuth (auth_type=OAUTH) — all optional. With no client_id we use Dynamic
    # Client Registration; provide id/secret for servers that don't support it.
    oauth_client_id: str | None = Field(default=None, max_length=512)
    oauth_client_secret: str | None = Field(default=None, max_length=2048)
    oauth_scope: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ConnectorCreate":
        if self.catalog_id is None:
            if not (self.name and self.url):
                raise ValueError("name and url are required without a catalog_id.")
            return self

        # ``auth_type`` carries a NONE default, so "absent" and "explicitly NONE"
        # are indistinguishable — treat NONE as absent rather than inventing a
        # sentinel for a field the catalog owns anyway.
        conflicting = [
            field
            for field, value in (
                ("name", self.name),
                ("url", self.url),
                ("credential", self.credential),
                ("header_name", self.header_name),
                ("oauth_client_id", self.oauth_client_id),
                ("oauth_client_secret", self.oauth_client_secret),
                ("oauth_scope", self.oauth_scope),
            )
            if value is not None
        ]
        if self.auth_type != MCPServerAuthType.NONE:
            conflicting.append("auth_type")
        if conflicting:
            raise ValueError(
                "catalog_id defines the connector; remove "
                + ", ".join(sorted(conflicting))
                + "."
            )
        return self


class ConnectionTestRequest(_ConnectorAuthFields):
    url: str = Field(min_length=1, max_length=2048)


class ConnectorAuthorizationResponse(BaseModel):
    """The provider authorization URL the browser should be sent to."""

    authorization_url: str


class OAuthCallbackRequest(BaseModel):
    """The provider's callback params, forwarded server-side by the frontend."""

    code: str
    state: str


class ConnectionTestResponse(BaseModel):
    server_name: str
    tools: list[dict[str, str]]


class ConnectorUpdate(BaseModel):
    """Partial update. Auth is edited as a whole (auth_type + its credential)
    so validation stays coherent; omit all three to leave auth unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    auth_type: MCPServerAuthType | None = None
    credential: str | None = Field(default=None, max_length=8192)
    header_name: str | None = Field(default=None, max_length=128)
    connected: bool | None = None

    @model_validator(mode="after")
    def _auth_fields_consistent(self) -> "ConnectorUpdate":
        # Auth is edited as a whole: changing the credential requires resending
        # auth_type, otherwise the credential would be silently ignored (the
        # endpoint only re-encrypts when auth_type changes). Reject that footgun.
        if self.auth_type is None:
            if self.credential is not None or self.header_name is not None:
                raise ValueError(
                    "To change credentials, include auth_type."
                )
            return self
        _require_auth_credentials(self.auth_type, self.credential, self.header_name)
        return self
