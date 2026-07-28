"""/api/connectors — user-created MCP Connectors (remote MCP servers).

Create validates the server by probing it (handshake + tools/list) before the
row is stored; the credential is encrypted at rest and never returned. The
worker resolves url + decrypted credential live by id at turn time.

OAuth connectors (auth_type=OAUTH) can't be probed pre-authorization: create
just registers/stores the OAuth client, then a separate authorize → provider →
callback flow obtains and stores the tokens (see ``app.services.mcp.oauth``).
"""

import json

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

from app.api.dependencies import get_db, get_redis
from app.auth import authenticate_and_get_user
from app.errors import ConnectorNameConflictError, ConnectorOAuthError
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.mcp_server import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    ConnectorAuthorizationResponse,
    ConnectorCatalogEntryResponse,
    ConnectorCreate,
    ConnectorResponse,
    ConnectorUpdate,
    OAuthCallbackRequest,
)
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.mcp import catalog, oauth
from app.services.mcp.probe import probe
from app.services.redis import oauth_state_key
from config import settings

router = APIRouter(tags=["connectors"])


@router.post("/connectors/connection-tests")
@limiter.limit("30/minute")
async def create_connection_test(
    request: Request,
    body: ConnectionTestRequest,
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectionTestResponse:
    """Probe a remote MCP server without saving — validates URL and lists tools."""
    credential = _credential_plaintext(body.auth_type, body.credential, body.header_name)
    result = await probe(body.url, body.auth_type, credential)
    return ConnectionTestResponse(server_name=result.server_name, tools=result.tools)


@router.get("/connectors/catalog")
async def list_connector_catalog(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[ConnectorCatalogEntryResponse]:
    """The official Connector directory — templates the caller can add.

    User-independent (every caller sees the same published entries); the client
    joins it against ``GET /connectors`` on ``catalog_id`` to tell an entry it
    has already added from one it hasn't.
    """
    entries = await catalog.list_published(db)
    return [ConnectorCatalogEntryResponse.model_validate(e) for e in entries]


@router.post("/connectors", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_connector(
    request: Request,
    body: ConnectorCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectorResponse:
    """Persist a Connector, from the official catalog or a custom definition.

    Probes the server first, except for OAuth — which can't be probed until the
    user authorizes (see ``/connectors/{id}/authorizations``). Adding the same
    catalog entry twice collides on the per-user name uniqueness and surfaces as
    the usual 409.
    """
    if body.catalog_id is not None:
        connector = await _build_catalog_connector(db, body.catalog_id, current_user.id)
    elif body.auth_type == MCPServerAuthType.OAUTH:
        connector = await _build_oauth_connector(body, current_user.id)
    else:
        credential = _credential_plaintext(
            body.auth_type, body.credential, body.header_name
        )
        result = await probe(body.url, body.auth_type, credential)
        connector = MCPServer(
            user_id=current_user.id,
            name=body.name,
            url=body.url,
            auth_type=body.auth_type,
            encrypted_credential=(
                encrypt_secret(credential) if credential is not None else None
            ),
            tool_count=len(result.tools),
            tools_preview=result.tools,
        )

    db.add(connector)
    # ``connector.name``, not ``body.name`` — the catalog path takes its name
    # from the entry, so the body's is None there.
    await _commit_or_conflict(db, connector.name)
    await db.refresh(connector)
    return _to_response(connector)


@router.post("/connectors/{connector_id}/authorizations")
@limiter.limit("30/minute")
async def create_connector_authorization(
    request: Request,
    connector_id: int,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectorAuthorizationResponse:
    """Begin the OAuth authorization-code flow — returns the provider URL to open.

    Stores the PKCE verifier + identity under an opaque ``state`` in Redis; the
    callback (below) consumes it. Re-callable to reconnect a revoked connector.
    """
    connector = await _get_owned_connector(db, connector_id, current_user.id)
    if connector.auth_type != MCPServerAuthType.OAUTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="connector is not an OAuth connector",
        )
    redirect_uri = _require_redirect_uri()
    metadata = await oauth.discover_metadata(connector.url)
    client = _load_oauth_client(connector)
    pkce = oauth.new_pkce()
    state = oauth.new_state()
    await redis.set(
        oauth_state_key(state),
        json.dumps({
            "connector_id": connector.id,
            "user_id": current_user.id,
            "code_verifier": pkce.code_verifier,
            # Cache the discovered metadata so the callback exchanges the code
            # without repeating the multi-request discovery against the server.
            "metadata": metadata.model_dump_json(),
        }),
        ex=settings.mcp_oauth_state_ttl_seconds,
    )
    authorization_url = oauth.build_authorization_url(
        metadata, client,
        redirect_uri=redirect_uri, scope=connector.oauth_scope,
        state=state, pkce=pkce,
    )
    return ConnectorAuthorizationResponse(authorization_url=authorization_url)


@router.post("/connectors/oauth/callback")
@limiter.limit("30/minute")
async def oauth_callback(
    request: Request,
    body: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict[str, bool]:
    """Exchange the authorization code for tokens and store them.

    No auth dependency: the browser redirect carries no Bearer header, so the
    caller's identity comes from the opaque ``state`` (validated against Redis,
    one-shot). The frontend proxy forwards ``code``+``state`` here, then redirects
    the browser back to the Connectors page.
    """
    key = oauth_state_key(body.state)
    raw = await redis.get(key)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired authorization state",
        )
    await redis.delete(key)  # one-shot
    pending = json.loads(raw)

    connector = await db.get(MCPServer, pending["connector_id"])
    if connector is None or connector.user_id != pending["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="connector not found",
        )

    redirect_uri = _require_redirect_uri()
    # Reuse the metadata discovered in /authorize (cached in the state) so we
    # don't repeat discovery; fall back to a fresh discovery if it's absent.
    cached_metadata = pending.get("metadata")
    metadata = (
        OAuthMetadata.model_validate_json(cached_metadata)
        if cached_metadata
        else await oauth.discover_metadata(connector.url)
    )
    client = _load_oauth_client(connector)
    tokens = await oauth.exchange_code(
        metadata, client,
        code=body.code, redirect_uri=redirect_uri,
        code_verifier=pending["code_verifier"],
    )
    connector.oauth_tokens_encrypted = encrypt_secret(tokens.model_dump_json())

    # Now that we hold an access token, list the tools (best-effort) so the card
    # shows a count. The access token is a bearer for the probe.
    try:
        result = await probe(
            connector.url, MCPServerAuthType.BEARER, tokens.access_token,
        )
        connector.tool_count = len(result.tools)
        connector.tools_preview = result.tools
    except Exception:
        pass

    connector.connected = True
    await db.commit()
    return {"ok": True}


@router.get("/connectors")
async def list_connectors(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[ConnectorResponse]:
    """List the caller's Connectors, newest first."""
    stmt = (
        select(MCPServer)
        .where(MCPServer.user_id == current_user.id)
        .order_by(MCPServer.created_at.desc())
    )
    result = await db.execute(stmt)
    return [_to_response(row) for row in result.scalars()]


@router.patch("/connectors/{connector_id}")
@limiter.limit("30/minute")
async def update_connector(
    request: Request,
    connector_id: int,
    body: ConnectorUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectorResponse:
    """Update a Connector; re-probe when a non-OAuth URL or auth changes."""
    connector = await _get_owned_connector(db, connector_id, current_user.id)

    if body.name is not None:
        connector.name = body.name
    if body.connected is not None:
        connector.connected = body.connected

    # Switching an existing connector to OAuth needs the full discover +
    # client-registration + browser-authorize flow (see create); a PATCH can't
    # provision it, and letting it through would leave the row unauthorizable.
    if body.auth_type == MCPServerAuthType.OAUTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="changing a connector to OAuth isn't supported; create a new OAuth connector",
        )

    url_changed = body.url is not None and body.url != connector.url
    auth_changed = body.auth_type is not None
    if url_changed:
        connector.url = body.url

    if auth_changed:
        credential = _credential_plaintext(body.auth_type, body.credential, body.header_name)
        connector.auth_type = body.auth_type
        connector.encrypted_credential = (
            encrypt_secret(credential) if credential is not None else None
        )
        # New auth type is non-OAuth (guarded above), so any prior OAuth
        # client/tokens/scope are now meaningless — drop them.
        connector.oauth_client_encrypted = None
        connector.oauth_tokens_encrypted = None
        connector.oauth_scope = None
    else:
        credential = _decrypt_or_none(connector.encrypted_credential)

    # OAuth re-probe needs a live token (re-authorize instead); only non-OAuth
    # connectors are probed here.
    if (url_changed or auth_changed) and connector.auth_type != MCPServerAuthType.OAUTH:
        result = await probe(connector.url, connector.auth_type, credential)
        connector.tool_count = len(result.tools)
        connector.tools_preview = result.tools

    await _commit_or_conflict(db, connector.name)
    await db.refresh(connector)
    return _to_response(connector)


@router.delete("/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Delete a Connector (its encrypted credential/tokens go with the row)."""
    connector = await _get_owned_connector(db, connector_id, current_user.id)
    await db.delete(connector)
    await db.commit()


async def _build_catalog_connector(
    db: AsyncSession, catalog_id: int, user_id: int,
) -> MCPServer:
    """Instantiate an official catalog entry as this user's own Connector.

    Same two paths as a hand-added Connector, just with the fields copied from
    the entry: OAuth registers its client now (``/authorizations`` needs one
    stored), everything else is probed so a broken entry fails at add time.
    """
    entry = await catalog.get_published_entry(db, catalog_id)
    connector = catalog.build_connector(entry, user_id)
    # Reject a re-add *before* the OAuth registration below: DCR mints a client
    # at the provider, and a client created for a row that then 409s on the name
    # index is orphaned — never stored, never revoked. The unique index is still
    # the authority; this check just keeps the common retry off the network.
    await _reject_duplicate_name(db, user_id, connector.name)
    if connector.auth_type == MCPServerAuthType.OAUTH:
        connector.oauth_client_encrypted = await _register_oauth_client(
            url=connector.url, scope=connector.oauth_scope,
        )
    else:
        result = await probe(connector.url, connector.auth_type, None)
        connector.tool_count = len(result.tools)
        connector.tools_preview = result.tools
    return connector


async def _build_oauth_connector(body: ConnectorCreate, user_id: int) -> MCPServer:
    """Discover metadata + register/prepare the OAuth client (no tokens yet)."""
    return MCPServer(
        user_id=user_id,
        name=body.name,
        url=body.url,
        auth_type=MCPServerAuthType.OAUTH,
        oauth_client_encrypted=await _register_oauth_client(
            url=body.url,
            scope=body.oauth_scope,
            client_id=body.oauth_client_id,
            client_secret=body.oauth_client_secret,
        ),
        oauth_scope=body.oauth_scope,
        connected=False,  # not handed to the agent until authorized
        tool_count=0,
    )


async def _register_oauth_client(
    *,
    url: str,
    scope: str | None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """The encrypted OAuth client for *url* — the manual one, else via DCR.

    Shared by the custom and catalog paths so both store a client in the same
    shape; ``/authorizations`` reads it back and would fail loudly on a row
    without one.
    """
    redirect_uri = _require_redirect_uri()
    metadata = await oauth.discover_metadata(url)
    client = await oauth.ensure_client(
        metadata,
        redirect_uri=redirect_uri,
        scope=scope,
        client_id=client_id,
        client_secret=client_secret,
    )
    return encrypt_secret(client.model_dump_json())


def _require_redirect_uri() -> str:
    if not settings.mcp_oauth_redirect_uri:
        raise ConnectorOAuthError("OAuth is not configured (mcp_oauth_redirect_uri).")
    return settings.mcp_oauth_redirect_uri


def _to_response(connector: MCPServer) -> ConnectorResponse:
    response = ConnectorResponse.model_validate(connector)
    response.authorized = (
        connector.auth_type != MCPServerAuthType.OAUTH
        or connector.oauth_tokens_encrypted is not None
    )
    return response


def _credential_plaintext(
    auth_type: MCPServerAuthType, credential: str | None, header_name: str | None,
) -> str | None:
    """The credential in the shape the probe/worker consume and we store encrypted.

    BEARER → the token; HEADER → a JSON ``{"header","value"}`` pair; else None.
    """
    if auth_type == MCPServerAuthType.BEARER:
        return credential
    if auth_type == MCPServerAuthType.HEADER:
        return json.dumps({"header": header_name, "value": credential})
    return None


def _decrypt_or_none(ciphertext: str | None) -> str | None:
    return decrypt_secret(ciphertext) if ciphertext else None


def _load_oauth_client(connector: MCPServer) -> OAuthClientInformationFull:
    """Decrypt the connector's stored OAuth client, or fail cleanly if absent.

    A well-formed OAuth connector always has one (create registers it and PATCH
    can't switch a connector to OAuth). A NULL column means a malformed row —
    surface a typed :class:`ConnectorOAuthError` instead of letting
    ``decrypt_secret("")`` raise a raw Fernet ``InvalidToken`` (an unhandled 500).
    """
    if not connector.oauth_client_encrypted:
        raise ConnectorOAuthError(
            "connector has no OAuth client registration; re-create it."
        )
    return OAuthClientInformationFull.model_validate_json(
        decrypt_secret(connector.oauth_client_encrypted)
    )


async def _get_owned_connector(
    db: AsyncSession, connector_id: int, user_id: int,
) -> MCPServer:
    stmt = select(MCPServer).where(
        MCPServer.id == connector_id, MCPServer.user_id == user_id,
    )
    result = await db.execute(stmt)
    connector = result.scalar_one_or_none()
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="connector not found",
        )
    return connector


async def _reject_duplicate_name(db: AsyncSession, user_id: int, name: str) -> None:
    """Raise the same 409 the unique index would, without hitting the network.

    Advisory only — a concurrent insert still lands on
    ``uq_mcp_server_user_id_name`` in :func:`_commit_or_conflict`.
    """
    stmt = select(MCPServer.id).where(
        MCPServer.user_id == user_id, MCPServer.name == name,
    )
    if (await db.execute(stmt)).first() is not None:
        raise ConnectorNameConflictError(
            f"a connector named {name!r} already exists for this user"
        )


async def _commit_or_conflict(db: AsyncSession, name: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConnectorNameConflictError(
            f"a connector named {name!r} already exists for this user"
        ) from exc
