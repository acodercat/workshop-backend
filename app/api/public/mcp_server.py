"""/api/connectors — user-created MCP Connectors (remote MCP servers).

Create validates the server by probing it (handshake + tools/list) before the
row is stored; the credential is encrypted at rest and never returned. The
worker resolves url + decrypted credential live by id at turn time.

OAuth connectors (auth_type=OAUTH) can't be probed pre-authorization: create
just registers/stores the OAuth client, then a separate authorize → provider →
callback flow obtains and stores the tokens (see ``app.services.mcp.authorization``).
"""

import json
import logging
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_redis
from app.auth import authenticate_and_get_user
from app.database import release_connection
from app.errors import (
    ConnectorNotAuthorizedError,
    ConnectorNotFoundError,
    ConnectorNotOAuthError,
    ConnectorOAuthChangeUnsupportedError,
    ConnectorOAuthError,
    ConnectorUrlConflictError,
)
from app.models.connector_category import ConnectorCategory
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.mcp_server import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    ConnectorAuthorizationResponse,
    ConnectorCatalogEntryResponse,
    ConnectorCategoryResponse,
    ConnectorCreate,
    ConnectorResponse,
    ConnectorUpdate,
    OAuthCallbackRequest,
)
from app.services import localized_text
from app.services.connector import catalog
from app.services.crypto import encrypt_secret, open_secret
from app.services.localized_text import DEFAULT_LOCALE, normalize_locale
from app.services.mcp import authorization, grant, oauth
from app.services.mcp.probe import ProbeResult, probe
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
    credential = _credential_plaintext(body.auth_type, body.credential, body.header_name)
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
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CIMD is not configured",
        )
    return oauth.client_metadata_document(
        settings.mcp_client_metadata_url,
        redirect_uri=authorization.redirect_uri(),
        client_name=settings.mcp_client_name,
    )


@router.get("/connectors/catalog")
async def list_connector_catalog(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    locale: str = Query(default=DEFAULT_LOCALE),
) -> list[ConnectorCatalogEntryResponse]:
    """The official Connector catalog — templates the caller can add.

    User-independent (every caller sees the same published entries); the client
    joins it against ``GET /connectors`` on ``catalog_id`` to tell an entry it
    has already added from one it hasn't.

    ``name`` and ``description`` are resolved for the ``locale`` query param
    (the frontend proxy forwards the app UI language, as it does for skills):
    an entry carrying a translation returns it, everything else the stored
    default. The maps themselves are never exposed.
    """
    resolved_locale = normalize_locale(locale)
    return [
        _localized_catalog_response(entry, resolved_locale)
        for entry in await catalog.list_published(db)
    ]


def _localized_catalog_response(
    entry: MCPServerCatalogEntry, locale: str
) -> ConnectorCatalogEntryResponse:
    """One entry as the client sees it, in *locale*.

    Built field-by-field rather than validated-then-patched: the response is
    what the client renders, and assigning over a validated copy is how a
    field quietly keeps the untranslated value when it is renamed.
    """
    response = ConnectorCatalogEntryResponse.model_validate(entry)
    response.name = localized_text.resolve(entry.name, entry.name_i18n, locale)
    response.description = localized_text.resolve_optional(
        entry.description, entry.description_i18n, locale
    )
    return response


@router.get("/connectors/catalog/categories")
async def list_connector_categories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    locale: str = Query(default=DEFAULT_LOCALE),
) -> list[ConnectorCategoryResponse]:
    """The tabs the connectors page groups catalog entries under.

    Under ``/connectors/catalog`` rather than beside it, because that is
    what these group: a user's own Connectors live behind the pinned
    personal tab and are not the platform's to classify.

    **The array order is the tab order** — rendered as given, not sorted
    by name. An unresolvable ``category_id`` and an empty category are
    both the client's to handle; the reasoning is written out on
    ``list_skill_categories``, which answers the same contract.
    """
    resolved_locale = normalize_locale(locale)
    rows = (
        await db.scalars(
            select(ConnectorCategory)
            .where(ConnectorCategory.published.is_(True))
            .order_by(ConnectorCategory.sort_order, ConnectorCategory.id)
        )
    ).all()
    return [_localized_connector_category_response(row, resolved_locale) for row in rows]


def _localized_connector_category_response(
    category: ConnectorCategory, locale: str
) -> ConnectorCategoryResponse:
    """One tab as the client sees it, in *locale*."""
    response = ConnectorCategoryResponse.model_validate(category)
    response.name = localized_text.resolve(category.name, category.name_i18n, locale)
    return response


@router.post("/connectors", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_connector(
    request: Request,
    body: ConnectorCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    locale: str = Query(default=DEFAULT_LOCALE),
) -> ConnectorResponse:
    """Persist a Connector, from the official catalog or a custom definition.

    Probes the server first, except for OAuth — which can't be probed until the
    user authorizes (see ``/connectors/{id}/authorizations``). Adding the same
    catalog entry twice collides on the per-user URL uniqueness and surfaces as
    the usual 409.

    ``locale`` decides which of a catalog entry's names is copied onto the new
    row — the one the user was reading. It is a snapshot from then on.
    """
    if body.catalog_id is not None:
        connector = await _build_catalog_connector(
            db, body.catalog_id, current_user.id, normalize_locale(locale)
        )
    elif body.auth_type == MCPServerAuthType.OAUTH:
        connector = await _build_oauth_connector(db, body, current_user.id)
    else:
        credential = _credential_plaintext(body.auth_type, body.credential, body.header_name)
        await release_connection(db)
        result = await probe(body.require_url(), body.auth_type, credential)
        connector = MCPServer(
            user_id=current_user.id,
            name=body.name,
            url=body.url,
            auth_type=body.auth_type,
            encrypted_credential=(encrypt_secret(credential) if credential is not None else None),
        )
        _apply_probe(connector, result)

    db.add(connector)
    # ``connector.name``, not ``body.name`` — the catalog path takes its name
    # from the entry, so the body's is None there.
    await _commit_or_conflict(db, connector.url)
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

    Re-callable to reconnect a revoked connector; see
    :func:`app.services.mcp.authorization.start`.
    """
    connector = await _get_owned_connector(db, connector_id, current_user.id)
    if connector.auth_type != MCPServerAuthType.OAUTH:
        raise ConnectorNotOAuthError("connector is not an OAuth connector")
    return ConnectorAuthorizationResponse(
        authorization_url=await authorization.start(db, redis, connector)
    )


@router.post("/connectors/oauth/callback")
@limiter.limit("30/minute")
async def oauth_callback(
    request: Request,
    body: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(authenticate_and_get_user),
) -> dict[str, bool]:
    """Exchange the authorization code for tokens and store them.

    The frontend proxy forwards ``code`` + ``state`` here with the session
    cookie the provider's top-level redirect carried back, then sends the
    browser to the Connectors page. Why the cookie matters is
    :func:`app.services.mcp.authorization.complete`'s to say.
    """
    connector = await authorization.complete(
        db,
        redis,
        user_id=current_user.id,
        state=body.state,
        code=body.code,
        iss=body.iss,
    )
    await _probe_after_authorization(db, connector)
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
    # Bound as values rather than flags. The ``is not None`` test that makes
    # each one a *change* is the same test that makes it safe to use below, and
    # a bool throws that away — every later use then reads as possibly-``None``
    # even though reaching it proves otherwise.
    changed_url = body.url if body.url != connector.url else None
    changed_auth = body.auth_type
    url_changed = changed_url is not None
    auth_changed = changed_auth is not None
    _reject_unprovisionable(connector, body, url_changed=url_changed)
    if changed_url is not None:
        # Ask before probing: pointing an existing Connector at a server the
        # user already has is a 409, and finding that out from the index after
        # a network round trip would report it as a failed probe instead.
        await _reject_duplicate_url(db, current_user.id, changed_url, excluding=connector.id)

    # What to probe is decided by the row *as it will be*, not as it is: an OAuth
    # Connector switching to BEARER is probed (its new auth type is not OAuth),
    # and reading ``connector.auth_type`` before the switch would say the
    # opposite. Hoisting the values names them once, so the decision below and
    # the write further down cannot disagree about what this PATCH produces.
    next_url = changed_url if changed_url is not None else connector.url
    next_auth_type = changed_auth if changed_auth is not None else connector.auth_type
    new_credential = (
        _credential_plaintext(changed_auth, body.credential, body.header_name)
        if changed_auth is not None
        else None
    )
    # OAuth re-probe needs a live token (re-authorize instead); only non-OAuth
    # connectors are probed here. The stored credential is opened only for that
    # probe, so a row sealed under a rotated key stays renamable and
    # disconnectable rather than failing every PATCH on a decrypt it never uses.
    probed = None
    if (url_changed or auth_changed) and next_auth_type != MCPServerAuthType.OAUTH:
        probe_credential = (
            new_credential if auth_changed else _decrypt_or_none(connector.encrypted_credential)
        )
        await release_connection(db)
        probed = await probe(next_url, next_auth_type, probe_credential)
        connector = await _get_owned_connector(db, connector_id, current_user.id)

    # Nothing above this line has touched the row — that is what makes the
    # release safe, and what keeps a failed probe from stranding half a PATCH.
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
            await authorization.release(redis, connector, defer=background_tasks.add_task)
        connector.connected = body.connected
    if body.disabled_tools is not None:
        # Not validated against the live tool list: that would cost a network
        # round-trip on every PATCH, and an unmatched name is inert anyway.
        connector.disabled_tools = body.disabled_tools
    if body.resources_enabled is not None:
        # Not gated on the server having any resources, for the same reason: the
        # answer needs a round-trip, it can change without this row changing, and
        # a server with none simply lists none. Enabling access to nothing is
        # harmless; refusing it on a stale cache is not.
        connector.resources_enabled = body.resources_enabled
    if changed_url is not None:
        # Guarded by the probe above, which every surviving URL change reaches:
        # OAuth rows are refused by ``_reject_unprovisionable``, and no other
        # auth type skips it.
        connector.url = changed_url
        # The grant was made about a *server*, and the URL is which server this
        # is — the same identity an OAuth token is bound to, which is why
        # repointing an OAuth row is refused outright above. The other auth
        # types may move, so the grant does not move with them: carrying it
        # across would hand a server the user has not considered read access to
        # everything it holds, on a decision they made about a different one.
        #
        # Unlike ``disabled_tools``, which carries over safely because it only
        # ever withholds. A PATCH that repoints *and* enables in one request
        # ends up off; nothing the UI does produces that, and off is the
        # direction to be wrong in.
        connector.resources_enabled = False
    if changed_auth is not None:
        await _apply_auth_change(
            redis,
            background_tasks,
            connector,
            next_auth_type,
            new_credential,
        )
    if probed is not None:
        _apply_probe(connector, probed)

    await _commit_or_conflict(db, connector.url)
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
        raise ConnectorNotAuthorizedError(
            "this connector isn't authorized; authorize it rather than connecting it"
        )


def _reject_unprovisionable(
    connector: MCPServer,
    body: ConnectorUpdate,
    *,
    url_changed: bool,
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
        raise ConnectorOAuthChangeUnsupportedError(
            "changing a connector to OAuth isn't supported; create a new OAuth connector"
        )
    if url_changed and connector.auth_type == MCPServerAuthType.OAUTH:
        raise ConnectorOAuthChangeUnsupportedError(
            "changing an OAuth connector's URL isn't supported; create a new OAuth connector"
        )


async def _apply_auth_change(
    redis: aioredis.Redis,
    background_tasks: BackgroundTasks,
    connector: MCPServer,
    auth_type: MCPServerAuthType,
    credential: str | None,
) -> None:
    """Swap the Connector to *auth_type* and *credential*.

    Handed both rather than deriving them from the body, because the caller
    needs the same pair earlier — to probe with, before this writes anything.
    One derivation, one meaning, so the row that gets written and the row that
    was probed cannot disagree.

    The new auth type is never OAuth (:func:`_reject_unprovisionable` refused
    that), so every trace of the old one is now unreachable and goes: the
    authorization, and then the standing to ask for another. Both halves, because
    this is one of the three ways a user ends an authorization.
    """
    connector.auth_type = auth_type
    connector.encrypted_credential = encrypt_secret(credential) if credential is not None else None
    await authorization.release(redis, connector, defer=background_tasks.add_task)
    authorization.forget_how_to_authorize(connector)


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
    await release_connection(db)
    result = await probe(connector.url, auth_type, credential)
    connector = await _get_owned_connector(db, connector_id, current_user.id)
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
    await authorization.release(redis, connector, defer=background_tasks.add_task)
    await db.delete(connector)
    await db.commit()


async def _build_catalog_connector(
    db: AsyncSession,
    catalog_id: int,
    user_id: int,
    locale: str,
) -> MCPServer:
    """Instantiate an official catalog entry as this user's own Connector.

    Same two paths as a hand-added Connector, just with the fields copied from
    the entry: OAuth registers its client now (``/authorizations`` needs one
    stored), everything else is probed so a broken entry fails at add time.
    """
    entry = await catalog.get_published_entry(db, catalog_id)
    connector = catalog.build_connector(entry, user_id, locale)
    await _reject_duplicate_url(db, user_id, connector.url)
    # Decrypted before the connection is released: ``entry`` belongs to this
    # session, and a Fernet failure should surface as the usual
    # credential-unreadable error, not as a lazy-load surprise later.
    client_secret = (
        open_secret(
            entry.oauth_client_secret_encrypted,
            what=f"catalog entry {entry.name!r} OAuth client secret",
        )
        if entry.oauth_client_secret_encrypted is not None
        else None
    )
    # ``connector`` is unsaved and not in the session, so this commits the two
    # reads above and nothing else.
    await release_connection(db)
    if connector.auth_type == MCPServerAuthType.OAUTH:
        # An entry carrying a pre-registered app (a provider without DCR/CIMD —
        # Figma requires one) hands it in here, and ``ensure_client`` treats it
        # as a manual client. Entries without one keep the DCR/CIMD path
        # exactly as before.
        (
            connector.oauth_client_encrypted,
            connector.oauth_metadata,
        ) = await authorization.register_client(
            url=connector.url,
            scope=connector.oauth_scope,
            client_id=entry.oauth_client_id,
            client_secret=client_secret,
        )
    else:
        result = await probe(connector.url, connector.auth_type, None)
        _apply_probe(connector, result)
    return connector


async def _build_oauth_connector(
    db: AsyncSession,
    body: ConnectorCreate,
    user_id: int,
) -> MCPServer:
    """Discover metadata + register/prepare the OAuth client (no tokens yet)."""
    url = body.require_url()
    await _reject_duplicate_url(db, user_id, url)
    await release_connection(db)
    client, documents = await authorization.register_client(
        url=url,
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
        oauth_metadata=documents,
        oauth_scope=body.oauth_scope,
        connected=False,  # not handed to the agent until authorized
        tool_count=0,
    )


def _to_response(connector: MCPServer) -> ConnectorResponse:
    response = ConnectorResponse.model_validate(connector)
    response.authorized = (
        connector.auth_type != MCPServerAuthType.OAUTH
        or connector.oauth_tokens_encrypted is not None
    )
    return response


async def _probe_after_authorization(db: AsyncSession, connector: MCPServer) -> None:
    """List the freshly-authorized Connector's tools, recording either outcome.

    Unlike the other probe call sites a failed probe does not raise — the user
    is mid-redirect and the grant is already stored, so failing the request
    would strand a Connector that is in fact authorized. But it must not report
    health it hasn't demonstrated either: holding a token says nothing about
    whether the server accepts its audience, or is up at all. So a failure is
    recorded rather than swallowed, and clears on the first turn that connects.

    The one refusal left is the row having been deleted while the probe ran.
    """
    result: ProbeResult | None
    try:
        result = await probe(connector.url, *_probe_credential(connector))
    except Exception:
        logger.warning(
            "Connector %s authorized but could not be reached; marking it",
            connector.id,
            exc_info=True,
        )
        result = None
    connector = await _get_owned_connector(db, connector.id, connector.user_id)
    if result is None:
        connector.last_connect_failed = True
    else:
        _apply_probe(connector, result)


def _apply_probe(connector: MCPServer, result: ProbeResult) -> None:
    """Copy everything a probe learned onto the row.

    Five paths probe a Connector (create, catalog add, OAuth callback, auth
    change, explicit re-list). Adding a probe-derived field to only some of them
    is a silent staleness bug, so they all go through here.

    Only probe-derived fields, though — the sentence above is about *learning*,
    not about the row. ``disabled_tools`` and ``resources_enabled`` are what the
    user decided, and a probe is in no position to revise either: writing them
    here would let re-listing a server's tools quietly re-grant access somebody
    had switched off.
    """
    connector.tool_count = len(result.tools)
    connector.tools_preview = result.tools
    connector.advertised_icons = result.icons
    connector.description = result.description
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
    of its own. Raises when an OAuth Connector holds no tokens yet (a probe
    would be indistinguishable from an unauthenticated one) and when the token
    it holds is past its recorded lifetime: presenting it produces a 401 that
    ``probe`` reports as an unreachable server, blaming the MCP server for a
    fact already on the row. Renewal is the worker's, on demand during a turn
    — this surface steers instead of renewing. "Recorded lifetime" rather
    than "expired", because for providers that state none the deadline is an
    assumption (``grant.ASSUMED_TOKEN_LIFETIME``).
    """
    if connector.auth_type != MCPServerAuthType.OAUTH:
        return connector.auth_type, _decrypt_or_none(connector.encrypted_credential)
    tokens = grant.stored_tokens(connector)
    if tokens is None:
        raise ConnectorOAuthError("This Connector is not authorized yet; authorize it first.")
    expires_at = connector.oauth_tokens_expires_at
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ConnectorOAuthError(
            "This Connector's access token is past its recorded lifetime; "
            "running a turn renews it automatically, or re-authorize it here."
        )
    return MCPServerAuthType.BEARER, tokens.access_token


def _credential_plaintext(
    auth_type: MCPServerAuthType,
    credential: str | None,
    header_name: str | None,
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
    return open_secret(ciphertext, what="credential") if ciphertext else None


async def _get_owned_connector(
    db: AsyncSession,
    connector_id: int,
    user_id: int,
) -> MCPServer:
    """The caller's Connector, read fresh — or ``connector_not_found``.

    A handler that probes an existing row reads it again here after the probe:
    the probe ran with the connection released, so the row may have been
    deleted or edited meanwhile, and writing to the copy from before turns a
    delete into a ``StaleDataError`` 500 instead of this 404.
    ``populate_existing`` is what makes the second read fresh rather than the
    identity map's pre-probe copy — and why it comes before any change to the
    row, which it would overwrite.
    """
    stmt = (
        select(MCPServer)
        .where(MCPServer.id == connector_id, MCPServer.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    connector = result.scalar_one_or_none()
    if connector is None:
        raise ConnectorNotFoundError("Connector not found.")
    return connector


async def _reject_duplicate_url(
    db: AsyncSession, user_id: int, url: str, *, excluding: int | None = None
) -> None:
    """Raise the same 409 the unique index would, without hitting the network.

    "Without hitting the network" is the whole point on the OAuth paths, and
    why both of them ask before they register: dynamic registration mints a
    client *at the provider*, and a client minted for a row that then collides
    on the index is orphaned — never stored, so never revocable, and a user
    re-adding the same server a few times leaves one behind each time.

    *excluding* is the row being edited, so a PATCH that leaves the URL where
    it already is does not collide with itself.

    Advisory only — a concurrent insert still lands on
    ``uq_mcp_server_user_id_url`` in :func:`_commit_or_conflict`.
    """
    stmt = select(MCPServer.id).where(
        MCPServer.user_id == user_id,
        MCPServer.url == url,
    )
    if excluding is not None:
        stmt = stmt.where(MCPServer.id != excluding)
    if (await db.execute(stmt)).first() is not None:
        raise ConnectorUrlConflictError(f"a connector for {url} already exists for this user")


async def _commit_or_conflict(db: AsyncSession, url: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConnectorUrlConflictError(
            f"a connector for {url} already exists for this user"
        ) from exc
