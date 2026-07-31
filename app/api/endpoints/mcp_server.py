"""/api/connectors — user-created MCP Connectors (remote MCP servers).

Create validates the server by probing it (handshake + tools/list) before the
row is stored; the credential is encrypted at rest and never returned. The
worker resolves url + decrypted credential live by id at turn time.

OAuth connectors (auth_type=OAUTH) can't be probed pre-authorization: create
just registers/stores the OAuth client, then a separate authorize → provider →
callback flow obtains and stores the tokens (see ``app.services.mcp.oauth``).
"""

import json
import logging
import time

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)

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
from app.services.mcp import catalog, grant, oauth
from app.services.mcp.probe import ProbeResult, probe
from app.services.redis import oauth_current_state_key, oauth_state_key
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connectors"])


@router.post("/connectors/connection-tests")
@limiter.limit("30/minute")
async def create_connection_test(
    request: Request,
    body: ConnectionTestRequest,
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectionTestResponse:
    """Probe a remote MCP server without saving — validates URL and lists tools."""
    credential = _credential_plaintext(
        body.auth_type, body.credential, body.header_name
    )
    result = await probe(body.url, body.auth_type, credential)
    return ConnectionTestResponse(server_name=result.server_name, tools=result.tools)


@router.get("/connectors/client-metadata")
@limiter.exempt
async def get_client_metadata() -> dict:
    """Our OAuth client metadata document, fetched by authorization servers (CIMD).

    Public by nature — the caller is a third-party authorization server
    resolving our ``client_id``, which *is* this URL. Rate-limit exempt because
    every user authorizing a Connector drives a fetch from the same provider IP.
    404 when unconfigured, so a half-configured deploy can't publish an identity
    naming a redirect URI it doesn't serve.
    """
    if not settings.mcp_client_metadata_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="CIMD is not configured",
        )
    return oauth.client_metadata_document(
        settings.mcp_client_metadata_url, redirect_uri=_require_redirect_uri(),
    )


@router.get("/connectors/catalog")
async def list_connector_catalog(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[ConnectorCatalogEntryResponse]:
    """The official Connector catalog — templates the caller can add.

    User-independent (every caller sees the same published entries); the client
    joins it against ``GET /connectors`` on ``catalog_id`` to tell an entry it
    has already added from one it hasn't.
    """
    entries = await catalog.list_published(db)
    return [ConnectorCatalogEntryResponse.model_validate(entry) for entry in entries]


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
        connector = await _build_oauth_connector(db, body, current_user.id)
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
        )
        _apply_probe(connector, result)

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
    discovered = await oauth.discover_authorization(connector.url)
    metadata = discovered.metadata
    client = await _client_for_authorization(connector, discovered, db)
    pkce = oauth.new_pkce()
    state = oauth.new_state()
    await redis.set(
        oauth_state_key(state),
        json.dumps({
            "connector_id": connector.id,
            "user_id": current_user.id,
            "code_verifier": pkce.code_verifier,
            # Recorded with the verifier because RFC 9207 says so: the check
            # is only worth anything if the expected issuer came from the
            # metadata document this flow validated, not from the response.
            "issuer": discovered.issuer_as_published,
            "iss_advertised": discovered.iss_parameter_supported,
            # Carried so the callback exchanges the code without repeating the
            # multi-request discovery, and then stores it on the row for a later
            # renewal to aim its rotation at.
            "discovered": discovered.as_stored(),
        }),
        ex=settings.mcp_oauth_state_ttl_seconds,
    )
    # Supersedes any flow already in the air for this Connector; the callback
    # checks it, so an older tab finishing later cannot land on top of this.
    await redis.set(
        oauth_current_state_key(connector.id),
        state,
        ex=settings.mcp_oauth_state_ttl_seconds,
    )
    authorization_url = oauth.build_authorization_url(
        metadata, client,
        server_url=connector.url,
        redirect_uri=redirect_uri,
        scope=discovered.scope_for(connector.oauth_scope),
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
    raw = await _consume_pending_authorization(redis, body.state)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired authorization state",
        )
    pending = json.loads(raw)
    # First thing the record is used for: a response from an authorization
    # server other than the one this flow was started with must not be acted
    # on at all — not exchanged, and not reported back from. Ahead of the
    # staleness check below, which is about which of two honest flows the user
    # meant; this one is about whether to trust the response at all.
    oauth.require_matching_issuer(
        body.iss,
        recorded=pending.get("issuer"),
        advertised=bool(pending.get("iss_advertised")),
    )
    await _reject_an_unawaited_authorization(redis, body.state, pending)

    connector = await db.get(MCPServer, pending["connector_id"])
    if connector is None or connector.user_id != pending["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="connector not found",
        )

    redirect_uri = _require_redirect_uri()
    # Reuse what /authorize discovered (carried in the state) rather than
    # repeating it; rediscover only if the state predates this field.
    stored = pending.get("discovered") or (
        await oauth.discover_authorization(connector.url)
    ).as_stored()
    metadata = OAuthMetadata.model_validate(stored["authorization_server"])
    # The same column in both forms: the client to exchange with, and the
    # ciphertext to prove afterwards that it is still the row's client.
    client = _load_oauth_client(connector)
    authorizing_client = connector.oauth_client_encrypted
    tokens = await oauth.exchange_code(
        metadata, client,
        code=body.code, server_url=connector.url, redirect_uri=redirect_uri,
        code_verifier=pending["code_verifier"],
    )
    # Rebound deliberately: everything read before the exchange is stale by
    # now, and the row this write belongs on is the one held under the lock.
    connector = await _locked_connector_for_grant(
        db, redis, pending, body.state, authorizing_client,
    )
    grant.store_tokens(connector, tokens)
    # Where a later renewal aims its rotation. Without it there is nothing safe
    # to guess from, so renewal would leave the Connector alone.
    connector.oauth_metadata = stored
    connector.connected = True
    # A fresh grant is exactly what the flag was asking for.
    connector.oauth_reauthorization_required = False
    # Committed before the probe, not after it. The probe is a round-trip to
    # someone else's server and can take as long as they like; holding the
    # grant behind it means two authorizations racing — a second tab finishing
    # while the first is still probing — end with whichever *probe* returned
    # last, not whichever grant is newer.
    await db.commit()

    await _probe_after_authorization(connector, tokens.access_token)
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
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectorResponse:
    """Update a Connector; re-probe when a non-OAuth URL or auth changes."""
    connector = await _get_owned_connector(db, connector_id, current_user.id)
    url_changed = body.url is not None and body.url != connector.url
    auth_changed = body.auth_type is not None
    _reject_unprovisionable(connector, body, url_changed=url_changed)

    if body.name is not None:
        connector.name = body.name
    if body.connected is not None:
        if body.connected:
            _reject_connecting_without_a_grant(connector)
        else:
            # Disconnecting gives the authorization up rather than shelving it:
            # what the provider delegated to us goes back, and reconnecting asks
            # for it again. How to *ask* is kept, which is the whole difference
            # from deleting the Connector.
            await _release_authorization(redis, background_tasks, connector)
        connector.connected = body.connected
    if body.disabled_tools is not None:
        # Not validated against the live tool list: that would cost a network
        # round-trip on every PATCH, and an unmatched name is inert anyway.
        connector.disabled_tools = body.disabled_tools
    if url_changed:
        # Guarded by ``probe`` below, which every surviving URL change reaches:
        # OAuth rows are refused above, and no other auth type skips the probe.
        connector.url = body.url
    if auth_changed:
        credential = await _apply_auth_change(
            redis, background_tasks, connector, body,
        )
    else:
        credential = _decrypt_or_none(connector.encrypted_credential)

    # OAuth re-probe needs a live token (re-authorize instead); only non-OAuth
    # connectors are probed here.
    if (url_changed or auth_changed) and connector.auth_type != MCPServerAuthType.OAUTH:
        _apply_probe(
            connector, await probe(connector.url, connector.auth_type, credential),
        )

    await _commit_or_conflict(db, connector.name)
    await db.refresh(connector)
    return _to_response(connector)


def _reject_connecting_without_a_grant(connector: MCPServer) -> None:
    """Refuse to connect an OAuth Connector that holds no grant.

    Connecting is a promise that the next turn can use it, and an OAuth row with
    no tokens cannot serve one call: the worker sends no bearer, the server
    answers 401, and the turn records the Connector as unreachable — a diagnosis
    that points at the server rather than at the missing authorization. The
    remedy is the browser, so say so instead of accepting the flag.

    This state only became reachable when disconnecting began giving the grant
    up. Before that, ``connected`` was set true in one place — the authorization
    callback, after it had stored a grant — so the flag implied one; now the two
    are independent and the endpoint has to say which combinations mean
    something. Rows disconnected before that change still carry their grant and
    still reconnect with a single flag, which is the case this must not refuse.

    Only reachable through the API: the UI routes such a click to the authorize
    flow. That is exactly why it is here — the invariant should not depend on a
    client choosing to honour it.
    """
    if connector.auth_type != MCPServerAuthType.OAUTH:
        return
    if connector.oauth_tokens_encrypted is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this connector isn't authorized; authorize it rather than "
                "connecting it"
            ),
        )


def _reject_unprovisionable(
    connector: MCPServer, body: ConnectorUpdate, *, url_changed: bool,
) -> None:
    """Refuse the two changes a ``PATCH`` cannot carry out.

    Both need the full discover + client-registration + browser-authorize flow
    that ``create`` runs, and both would otherwise leave a row nothing can
    re-provision — authorizable only by deleting it and starting over.

    Switching *to* OAuth is one: there would be no client registration to
    authorize against. Repointing an OAuth Connector's URL is the other, for
    the mirror reason — an OAuth grant belongs to the resource it was issued
    for (that URL is literally the RFC 8707 ``resource`` the token is bound
    to), so moving it has to drop the grant, and the client registration under
    it, which the old authorization server issued and may have given a secret.

    Refusing says so up front, rather than half-clearing the row and leaving
    the user to discover that Reconnect now fails.
    """
    if body.auth_type == MCPServerAuthType.OAUTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "changing a connector to OAuth isn't supported; "
                "create a new OAuth connector"
            ),
        )
    if url_changed and connector.auth_type == MCPServerAuthType.OAUTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "changing an OAuth connector's URL isn't supported; "
                "create a new OAuth connector"
            ),
        )


async def _apply_auth_change(
    redis: aioredis.Redis,
    background_tasks: BackgroundTasks,
    connector: MCPServer,
    body: ConnectorUpdate,
) -> str | None:
    """Swap the Connector's credential, returning the plaintext for the probe.

    The new auth type is never OAuth (:func:`_reject_unprovisionable` refused
    that), so every trace of the old one is now unreachable and goes: the
    authorization, and then the standing to ask for another. Both halves, because
    this is one of the three ways a user ends an authorization.
    """
    credential = _credential_plaintext(
        body.auth_type, body.credential, body.header_name
    )
    connector.auth_type = body.auth_type
    connector.encrypted_credential = (
        encrypt_secret(credential) if credential is not None else None
    )
    await _release_authorization(redis, background_tasks, connector)
    _forget_how_to_authorize(connector)
    return credential


@router.post("/connectors/{connector_id}/tool-listings")
@limiter.limit("30/minute")
async def create_connector_tool_listing(
    request: Request,
    connector_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ConnectorResponse:
    """Re-list a saved Connector's tools and refresh the cached preview.

    ``tools_preview`` is only written at create (non-OAuth) and by the OAuth
    callback's best-effort probe, so it can be empty or lag the server — and
    the per-tool switches are rendered from it. This is how a user gets a
    current list without disconnecting and reconnecting.

    Probes with the stored credential; for OAuth the access token is the
    bearer, exactly as the callback does.
    """
    connector = await _get_owned_connector(db, connector_id, current_user.id)
    auth_type, credential = _probe_credential(connector)
    result = await probe(connector.url, auth_type, credential)
    _apply_probe(connector, result)
    await db.commit()
    await db.refresh(connector)
    return _to_response(connector)


@router.delete("/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Delete a Connector — its encrypted credential and grant go with the row."""
    connector = await _get_owned_connector(db, connector_id, current_user.id)
    # Before the row goes, because this is the last moment anything can read what
    # the provider has to be told. Clearing columns on a row about to be deleted
    # is redundant, and left in anyway: one call at all three endings is worth
    # more than saving three assignments at one of them.
    await _release_authorization(redis, background_tasks, connector)
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
    await _reject_duplicate_name(db, user_id, connector.name)
    if connector.auth_type == MCPServerAuthType.OAUTH:
        connector.oauth_client_encrypted, connector.oauth_metadata = (
            await _register_oauth_client(
                url=connector.url, scope=connector.oauth_scope,
            )
        )
    else:
        result = await probe(connector.url, connector.auth_type, None)
        _apply_probe(connector, result)
    return connector


async def _build_oauth_connector(
    db: AsyncSession, body: ConnectorCreate, user_id: int,
) -> MCPServer:
    """Discover metadata + register/prepare the OAuth client (no tokens yet)."""
    await _reject_duplicate_name(db, user_id, body.name)
    client, authorization = await _register_oauth_client(
        url=body.url,
        scope=body.oauth_scope,
        client_id=body.oauth_client_id,
        client_secret=body.oauth_client_secret,
    )
    return MCPServer(
        user_id=user_id,
        name=body.name,
        url=body.url,
        auth_type=MCPServerAuthType.OAUTH,
        oauth_client_encrypted=client,
        oauth_metadata=authorization,
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
) -> tuple[str, dict]:
    """The encrypted OAuth client for *url*, and the documents that minted it.

    Shared by the custom and catalog paths so both store a client in the same
    shape; ``/authorizations`` reads it back and would fail loudly on a row
    without one.

    The documents are returned with it, and stored, because a client belongs to
    the authorization server that issued it — a DCR secret is *that server's*
    secret. Without recording which one that was, a later authorization has no
    way to notice that the MCP server now points somewhere else, and would
    present the credential to whoever it points at now.
    """
    redirect_uri = _require_redirect_uri()
    discovered = await oauth.discover_authorization(url)
    client = await oauth.ensure_client(
        discovered.metadata,
        redirect_uri=redirect_uri,
        scope=discovered.scope_for(scope),
        client_id=client_id,
        client_secret=client_secret,
        client_metadata_url=settings.mcp_client_metadata_url or None,
    )
    return encrypt_secret(client.model_dump_json()), discovered.as_stored()


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


async def _release_authorization(
    redis: aioredis.Redis,
    background_tasks: BackgroundTasks,
    connector: MCPServer,
) -> None:
    """Give up the authorization this Connector holds — all of it, at once.

    One is held in three places and they have to be let go together: the grant on
    the row, the provider's record of it, and any flow still in the air that
    would produce a new one. Three things end an authorization — disconnecting,
    converting to a static credential, deleting — and each used to name its own
    subset, so each arrived at a different one and the gaps were covered only by
    accident: the callback's client-ciphertext fence happened to catch the
    conversion, a missing row happened to catch the delete. Either of those
    changing would have regressed a path with nothing to notice it.

    Every step is inert when there is nothing to do, so no caller needs a guard.
    A Connector with no grant has none to revoke and none to clear; one with no
    flow in the air has no key to delete; and every non-OAuth auth type is both.

    The Redis call is *not* guarded, unlike the grant read one line above, and
    the difference is the kind of failure. An unreadable grant is permanent and
    belongs to one row — swallowing it is what keeps that Connector deletable at
    all. Redis being unreachable is transient and global: it fails the request,
    nothing commits, and the queued revocation never reaches a response to be
    run from, so the ending is all-or-nothing and the user's retry is the whole
    remedy. Swallowing it would instead commit a disconnect while leaving the
    flow live — the one outcome this function exists to prevent. It also cannot
    matter while it lasts, because the callback reads Redis before anything else.

    What it deliberately does not touch is how to obtain the *next* one — see
    :func:`_forget_how_to_authorize`, which only the conversion wants.
    """
    _revoke_grant_afterwards(background_tasks, connector)
    grant.discard(connector)
    await _stop_awaiting_authorization(redis, connector)


def _forget_how_to_authorize(connector: MCPServer) -> None:
    """Drop what the row needs to obtain an authorization at all.

    The client registration, the scope and the discovery metadata: not the
    authorization but the standing to ask for one. Kept across a disconnect,
    which is what makes reconnecting a single browser round-trip; dropped when
    the Connector stops being an OAuth Connector, because then nothing can reach
    them again.

    The metadata is the one that bites. It is what a renewal aims its rotation
    at, so a row that keeps it is a row renewal would go on working after the
    user replaced its credential with a static one.

    Together with :func:`_release_authorization` this is every ``oauth_*``
    column, which is the pair a conversion calls — and the reason each has a
    docstring saying which half it owns, so a column added later has one obvious
    home rather than two plausible ones.
    """
    connector.oauth_client_encrypted = None
    connector.oauth_scope = None
    connector.oauth_metadata = None


def _revoke_grant_afterwards(
    background_tasks: BackgroundTasks, connector: MCPServer,
) -> None:
    """Arrange for the provider to be told that a grant is finished with.

    After the response, not before it, and that ordering is the point. Dropping
    our copy of the tokens is a local write the user is entitled to see happen
    at once; telling the provider is a request to a third party that may be
    slow or down, and making the user wait on it would turn "disconnect" into a
    spinner over someone else's outage. Nothing here depends on the outcome —
    our copy is gone either way — so a failure is a log line and no more.

    Reads what the request needs *now*, while the row is still loaded and its
    columns still populated, and hands the background task plain values. Its one
    caller clears those columns on the next line, which is why the ordering
    inside :func:`_release_authorization` is not incidental.

    Re-authorizing is deliberately not an ending. It also replaces a grant, but
    the replacement is already in hand, and RFC 7009 §2.1 lets a server revoke
    every token issued to this client for this user rather than just the one
    named — which on such a provider would revoke the grant we had just
    obtained. Ending the old authorization is worth less than that risk; a user
    switching accounts can revoke the old one at the provider.

    Does nothing when there is nothing to say — no grant, no stored
    authorization, a provider advertising no revocation endpoint, or a grant
    that cannot be read. The last is why the reads are guarded rather than left
    to raise: they are the first thing on these paths to need the encryption
    key, so a Connector sealed under a key that has since been rotated would
    become one the user can no longer disconnect *or* delete — the two things
    they most need to do with a Connector that is broken.
    """
    try:
        tokens = grant.stored_tokens(connector)
        authorization = grant.stored_authorization(connector)
    except Exception:
        logger.warning(
            "Connector %s: its stored grant could not be read, so the provider "
            "cannot be told it is finished with", connector.id, exc_info=True,
        )
        return
    if tokens is None or authorization is None:
        return
    metadata, client = authorization
    if metadata.revocation_endpoint is None:
        logger.info(
            "Connector %s: its authorization server offers no revocation "
            "endpoint; dropping our copy of the grant only", connector.id,
        )
        return
    background_tasks.add_task(
        _revoke_quietly, connector.id, metadata, client, tokens,
    )


async def _stop_awaiting_authorization(
    redis: aioredis.Redis, connector: MCPServer,
) -> None:
    """Give up on any authorization still in the air for this Connector.

    Clearing the grant is not enough on its own. A flow the user started earlier
    keeps its own ``state`` in Redis for the whole TTL, so it would come back
    minutes later, write a grant, and set ``connected`` again — their last action
    losing to their second-to-last. Disconnecting is where that bites hardest,
    because the client registration the callback fences on is deliberately kept,
    so nothing further along would have refused it.

    Deleting the pointer is what says "waiting for nothing", and
    :func:`_reject_an_unawaited_authorization` reads that as a refusal. The
    pointer rather than the pending records: it is the only one of them this
    request can name, and it covers *every* flow in the air at once rather than
    the newest — two tabs open means two live ``state`` values and only one is
    pointed at.

    Idempotent, and unconditional for the same reason the calls beside it are:
    a Connector with nothing in flight has no key here to delete.
    """
    await redis.delete(oauth_current_state_key(connector.id))


async def _revoke_quietly(
    connector_id: int,
    metadata: OAuthMetadata,
    client: OAuthClientInformationFull,
    tokens: OAuthToken,
) -> None:
    """Revoke a grant, treating every failure as nothing more than news.

    Total on purpose. It runs after the response has been sent, so there is
    nobody left to tell — and Starlette re-raises what a background task
    raises, which would log a request-shaped traceback for a request that
    already succeeded.
    """
    try:
        await oauth.revoke_grant(metadata, client, tokens=tokens)
    except Exception:
        logger.warning(
            "Connector %s: could not revoke its grant at the provider; the "
            "authorization may still be listed on the user's account",
            connector_id, exc_info=True,
        )


async def _probe_after_authorization(
    connector: MCPServer, access_token: str,
) -> None:
    """List the freshly-authorized Connector's tools, recording either outcome.

    Unlike the other probe call sites this one cannot raise — the user is
    mid-redirect and the grant is already stored, so failing the request would
    strand a Connector that is in fact authorized. But it must not report
    health it hasn't demonstrated either: holding a token says nothing about
    whether the server accepts its audience, or is up at all. So a failure is
    recorded rather than swallowed, and clears on the first turn that connects.
    """
    try:
        _apply_probe(
            connector,
            await probe(connector.url, MCPServerAuthType.BEARER, access_token),
        )
    except Exception:
        logger.warning(
            "Connector %s authorized but could not be reached; marking it",
            connector.id, exc_info=True,
        )
        connector.last_connect_failed = True


def _apply_probe(connector: MCPServer, result: ProbeResult) -> None:
    """Copy everything a probe learned onto the row.

    Five paths probe a Connector (create, catalog add, OAuth callback, auth
    change, explicit re-list). Adding a probe-derived field to only some of them
    is a silent staleness bug, so they all go through here.
    """
    connector.tool_count = len(result.tools)
    connector.tools_preview = result.tools
    connector.advertised_icons = result.icons
    # Reaching here means the handshake completed, which is what the worker's
    # per-turn connect proves too — so a user who fixes a broken URL sees the
    # badge go at once rather than after the next turn. Never set here: a
    # failed probe raises, and tells the user directly.
    connector.last_connect_failed = False


def _probe_credential(
    connector: MCPServer,
) -> tuple[MCPServerAuthType, str | None]:
    """How to authenticate a probe of *connector*, as (auth_type, secret).

    OAuth is probed as a bearer carrying the stored access token — the MCP
    server takes it the same way either route, and ``probe`` has no OAuth path
    of its own. Raises when an OAuth Connector holds no tokens yet, since a
    probe would then be indistinguishable from an unauthenticated one.
    """
    if connector.auth_type != MCPServerAuthType.OAUTH:
        return connector.auth_type, _decrypt_or_none(connector.encrypted_credential)
    if not connector.oauth_tokens_encrypted:
        raise ConnectorOAuthError(
            "This Connector is not authorized yet; authorize it first."
        )
    tokens = OAuthToken.model_validate_json(
        decrypt_secret(connector.oauth_tokens_encrypted)
    )
    return MCPServerAuthType.BEARER, tokens.access_token


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


async def _locked_connector_for_grant(
    db: AsyncSession,
    redis: aioredis.Redis,
    pending: dict,
    state: str,
    authorizing_client: str | None,
) -> MCPServer:
    """Re-read the Connector under a row lock, and refuse if it has moved on.

    Everything read before the code exchange was read across a round-trip to
    someone else's token endpoint, which can take as long as they like. Two
    things can happen in that time, and both end with this grant written onto a
    row it does not describe: the user starts another authorization and picks a
    different account, or they change the Connector's credentials — to a bearer
    token, which clears the OAuth columns, or to a different OAuth client of
    their own. The second is the worse one: it would put a refresh token back on
    a row the user had just converted away from OAuth.

    One comparison covers both credential cases, because both move
    ``oauth_client_encrypted``: a changed auth type clears it, a re-registration
    replaces it, and Fernet ciphertext differs even for identical plaintext. It
    is the client this flow's code was issued to; a grant belongs with it.

    What it does *not* cover is a disconnect, which keeps that column on purpose
    so reconnecting stays cheap. That one is caught by
    :func:`_reject_an_unawaited_authorization`, called just below — disconnecting
    deletes the pointer naming the flow, and a flow nobody is waiting for is
    refused.

    ``with_for_update`` holds the row until this transaction commits, so unlike
    a bare check nothing can land between asking and writing.
    ``populate_existing`` is what makes the answer true in the first place: on
    its own the lock only bypasses the identity-map *shortcut*, and the row it
    then reads is loaded onto the instance this session already holds —
    without overwriting the attributes loaded before the exchange. The query
    locks, the object still says what it said an hour ago, and every check
    below passes on stale values. Both, or neither is worth having.
    """
    connector = await db.get(
        MCPServer,
        pending["connector_id"],
        with_for_update=True,
        populate_existing=True,
    )
    if connector is None or connector.user_id != pending["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="connector not found",
        )
    if connector.oauth_client_encrypted != authorizing_client:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this connector's credentials changed while you were "
                "authorizing; start again"
            ),
        )
    await _reject_an_unawaited_authorization(redis, state, pending)
    return connector


async def _consume_pending_authorization(
    redis: aioredis.Redis, state: str,
) -> str | None:
    """Read the pending authorization for *state* and burn it, in one step.

    One-shot has to mean one *winner*. Read-then-delete let both halves of a
    doubled callback — a browser retrying the redirect, a proxy replaying it —
    pass validation; one would exchange the code and the other would fail on a
    code the provider had already consumed, showing the user an error for a
    Connector that then quietly works.

    A transaction rather than ``GETDEL``, which wants Redis 6.2 and would be
    the only command here to ask for a floor this deployment has never had to
    meet.
    """
    key = oauth_state_key(state)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        raw, _deleted = await pipe.execute()
    return raw


async def _reject_an_unawaited_authorization(
    redis: aioredis.Redis, state: str, pending: dict,
) -> None:
    """Refuse a flow the Connector is no longer waiting for.

    Two ways it can stop waiting, and the Redis key says which. Holding another
    ``state`` means the user restarted the flow — a second tab, or a first
    attempt that looked stuck — and each ``state`` stays independently valid, so
    the winner would otherwise be whichever *finished* last. Choosing an account
    in the newer tab and having the older one quietly overwrite it leaves the
    Connector signed in as someone the user did not last pick, with nothing on
    screen saying so.

    Holding nothing means the Connector was disconnected while this flow was in
    the air. Fail closed, and that is a deliberate change from reading an absent
    key as consent: disconnecting deletes it, so the flow that would otherwise
    come back and write a grant — and set ``connected`` — arrives to find nobody
    waiting. Nothing else produces an absent key beside a live pending record:
    both are written in the same handler with the same TTL, the pending one
    first, so it is always the first to expire.

    Every ending reaches this, because every ending goes through
    :func:`_release_authorization`. Two of them would also be caught further on —
    a deleted Connector 404s, a converted one moves the ciphertext
    :func:`_locked_connector_for_grant` compares — but those are consequences
    rather than intentions, and the third, disconnecting, has neither: it keeps
    the client registration on purpose. One rule that fires for all three beats
    three accidents, two of which happen to work.

    Asked twice: once on the way in, to spend nothing on a flow already known to
    be stale, and again from inside :func:`_locked_connector_for_grant`, where
    the answer keeps until the write.
    """
    current = await redis.get(oauth_current_state_key(pending["connector_id"]))
    if current == state:
        return
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this connector is no longer waiting to be authorized; "
                "connect it again to start over"
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "a newer authorization was started for this connector; "
            "finish that one instead"
        ),
    )


async def _client_for_authorization(
    connector: MCPServer,
    discovered: oauth.DiscoveredAuthorization,
    db: AsyncSession,
) -> OAuthClientInformationFull:
    """The client this authorization should use — the stored one, or a fresh one.

    Two things disqualify the stored client, and they disqualify it the same
    way: the credential it holds no longer opens the door we are about to knock
    on. Either it has passed its own ``client_secret_expires_at`` and the server
    now answers ``invalid_client``, or an authorization server other than the
    one the MCP server names today minted it — a DCR ``client_secret`` is *that*
    server's secret, and presenting it here would be the resource server
    collecting our credentials by a route the discovery guards cannot cover,
    since every URL involved is legitimately public.

    Neither is fatal. A client an authorization server minted can be minted
    again, against whichever server is authoritative now; the old one is never
    sent anywhere, so this is exactly as safe as the new Connector the user
    would otherwise have to build by hand. Only a manually configured client
    cannot be replaced — it is the operator's own OAuth app, and there is no
    way to make another.

    One case is deliberately outside all of this: an authorization server that
    revokes a registered client *early*, having stated no expiry. Nothing here
    can see that, so Reconnect hands the dead registration back and only
    deleting the Connector helps. Acting on it would mean re-minting whenever
    the row is flagged for re-authorization, and that flag does not record which
    of the two permanent errors set it — for ``invalid_grant``, the ordinary
    "your grant expired, sign in again", the client is in perfect health and
    replacing it on every routine re-authorization would be the bug.
    """
    client = _load_oauth_client(connector)
    unusable = _why_the_stored_client_is_unusable(connector, client, discovered)
    if unusable is None:
        return client
    if not _issued_by_an_authorization_server(client):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{unusable}; create a new OAuth connector",
        )
    logger.info("Connector %s: %s; registering again", connector.id, unusable)
    connector.oauth_client_encrypted = encrypt_secret(
        (
            await oauth.ensure_client(
                discovered.metadata,
                redirect_uri=_require_redirect_uri(),
                scope=discovered.scope_for(connector.oauth_scope),
                client_metadata_url=settings.mcp_client_metadata_url or None,
            )
        ).model_dump_json()
    )
    # Stored with it: a client belongs to the server that issued it, and for a
    # row that had no issuer recorded at all, this reconnect is what gives it
    # one. Without this the row would arrive here again every time.
    connector.oauth_metadata = discovered.as_stored()
    await db.commit()
    return _load_oauth_client(connector)


def _why_the_stored_client_is_unusable(
    connector: MCPServer,
    client: OAuthClientInformationFull,
    discovered: oauth.DiscoveredAuthorization,
) -> str | None:
    """Why this authorization cannot reuse the stored client, or ``None``.

    The no-issuer case is not the same question as the changed-issuer case, and
    they get different answers on purpose. A row with no issuer recorded is
    every OAuth connector that exists when this ships — the column is new, and
    an absent record is evidence of nothing. What is being protected is a
    credential, so a client that holds none is not what the rule is about: a
    bare client id (CIMD's is a public URL) offered to the wrong server is
    simply rejected there, and nothing of ours travels. A *changed* issuer is
    positive evidence that the MCP server repointed, which is worth stopping
    for whether or not a secret is at stake.
    """
    if _secret_expired(client):
        return "this connector's client registration expired"
    registered_issuer = (
        (connector.oauth_metadata or {}).get("authorization_server", {}).get("issuer")
    )
    if not registered_issuer:
        if not client.client_secret:
            return None
        return "this connector predates authorization-server binding"
    if str(discovered.metadata.issuer) != registered_issuer:
        return "this connector's authorization server changed"
    return None


def _issued_by_an_authorization_server(client: OAuthClientInformationFull) -> bool:
    """Whether a server minted this client, rather than an operator configuring it.

    Both fields come from a registration response (RFC 7591 §3.2.1) and from
    nowhere else — :func:`oauth.ensure_client` builds a manual client out of the
    operator's id and secret alone — so either one present is proof. Note the
    ``is not None``, where :func:`_secret_expired` reads the same field for
    truthiness: ``client_secret_expires_at=0`` is a server saying "never
    expires", which is no deadline but is still a server having spoken.

    A registration that states neither is read as manual, and its client is
    kept rather than replaced. That is the safe way to be wrong: the user is
    asked to make a new Connector, instead of an operator's OAuth app being
    swapped out behind their back.
    """
    return (
        client.client_id_issued_at is not None
        or client.client_secret_expires_at is not None
    )


def _secret_expired(client: OAuthClientInformationFull) -> bool:
    """Whether the client's own credential has passed its stated expiry.

    ``0`` means "never expires" per RFC 7591 §3.2.1, and absent means the same;
    only a non-zero value is a deadline.
    """
    expires_at = client.client_secret_expires_at
    return bool(expires_at) and expires_at <= int(time.time())


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

    "Without hitting the network" is the whole point on the OAuth paths, and
    why both of them ask before they register: dynamic registration mints a
    client *at the provider*, and a client minted for a row that then collides
    on the name index is orphaned — never stored, so never revocable, and a
    user re-adding the same server a few times leaves one behind each time.

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
