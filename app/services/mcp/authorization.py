"""The browser half of an OAuth grant: start it, complete it, end it.

:func:`start` registers what a flow needs (PKCE verifier, the issuer it
expects, the discovery documents) under an opaque ``state`` and returns the
provider URL; :func:`complete` redeems that ``state`` for a grant on the
row; :func:`release` gives a grant up. Renewing one is not here — that is
the agent worker's, on demand during a turn.

The endpoint module keeps what is HTTP: ownership checks, probes, the
response. Everything here is about which flow is live, which client a flow
may use, and what becomes of a grant — the rules that used to sit between
the routes.
"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import release_connection
from app.errors import (
    ConnectorAuthorizationExpiredError,
    ConnectorAuthorizationSupersededError,
    ConnectorChangedDuringAuthorizationError,
    ConnectorNotFoundError,
    ConnectorOAuthClientUnusableError,
    ConnectorOAuthError,
    OAuthNotConfiguredError,
)
from app.models.mcp_server import MCPServer
from app.services.crypto import encrypt_secret
from app.services.mcp import grant, oauth
from app.services.redis import oauth_current_state_key, oauth_state_key
from config import settings

logger = logging.getLogger(__name__)

Defer = Callable[..., Any]
"""Runs ``fn(*args)`` after the response — ``BackgroundTasks.add_task``."""


async def start(db: AsyncSession, redis: aioredis.Redis, connector: MCPServer) -> str:
    """Begin an authorization-code flow for *connector*; return the provider URL.

    Stores the PKCE verifier and what the callback must check under an opaque
    ``state``, and points the Connector at that ``state`` — superseding any
    flow already in the air, so an older tab finishing later cannot land on
    top of this one. May re-mint the client (see
    :func:`_client_for_authorization`), which commits.
    """
    redirect = redirect_uri()
    await release_connection(db)
    discovered = await oauth.discover_authorization(connector.url)
    client = await _client_for_authorization(db, connector, discovered)
    pkce = oauth.new_pkce()
    state = oauth.new_state()
    await redis.set(
        oauth_state_key(state),
        json.dumps(
            {
                "connector_id": connector.id,
                "user_id": connector.user_id,
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
            }
        ),
        ex=settings.mcp_oauth_state_ttl_seconds,
    )
    await redis.set(
        oauth_current_state_key(connector.id),
        state,
        ex=settings.mcp_oauth_state_ttl_seconds,
    )
    return oauth.build_authorization_url(
        discovered.metadata,
        client,
        server_url=connector.url,
        redirect_uri=redirect,
        scope=discovered.scope_for(connector.oauth_scope),
        state=state,
        pkce=pkce,
    )


async def complete(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user_id: int,
    state: str,
    code: str,
    iss: str | None,
) -> MCPServer:
    """Redeem *state* and *code* for a grant on the Connector; return the row.

    ``state`` must belong to the user finishing the flow (RFC 9700 §4.7):
    otherwise anyone could start an authorization on their own Connector,
    send the consent link to a victim, and have the victim's provider account
    granted to it. A state started by someone else is refused exactly like an
    expired one, after it is burned.

    Commits the grant before returning. The caller's probe is a round-trip to
    someone else's server and can take as long as they like; holding the
    grant behind it would mean two authorizations racing — a second tab
    finishing while the first is still probing — end with whichever *probe*
    returned last, not whichever grant is newer.
    """
    raw = await _consume_pending_authorization(redis, state)
    if raw is None:
        raise ConnectorAuthorizationExpiredError("invalid or expired authorization state")
    pending = json.loads(raw)
    if pending["user_id"] != user_id:
        raise ConnectorAuthorizationExpiredError("invalid or expired authorization state")
    # First thing the record is used for: a response from an authorization
    # server other than the one this flow was started with must not be acted
    # on at all — not exchanged, and not reported back from. Ahead of the
    # staleness check below, which is about which of two honest flows the user
    # meant; this one is about whether to trust the response at all.
    oauth.require_matching_issuer(
        iss,
        recorded=pending.get("issuer"),
        advertised=bool(pending.get("iss_advertised")),
    )
    await _reject_an_unawaited_authorization(redis, state, pending)

    connector = await db.get(MCPServer, pending["connector_id"])
    if connector is None or connector.user_id != user_id:
        raise ConnectorNotFoundError("Connector not found.")

    redirect = redirect_uri()
    # Everything below until ``_locked_connector_for_grant`` is somebody else's
    # server answering, and the row read above is re-read under a lock there
    # anyway — so the connection goes back rather than idling through it.
    await release_connection(db)
    stored = pending["discovered"]
    # The same column in both forms: the client to exchange with, and the
    # ciphertext to prove afterwards that it is still the row's client.
    client = _stored_client(connector)
    authorizing_client = connector.oauth_client_encrypted
    tokens = await oauth.exchange_code(
        OAuthMetadata.model_validate(stored["authorization_server"]),
        client,
        code=code,
        server_url=connector.url,
        redirect_uri=redirect,
        code_verifier=pending["code_verifier"],
    )
    # Rebound deliberately: everything read before the exchange is stale by
    # now, and the row this write belongs on is the one held under the lock.
    connector = await _locked_connector_for_grant(db, redis, pending, state, authorizing_client)
    grant.store_tokens(connector, tokens)
    # Where a later renewal aims its rotation. Without it there is nothing safe
    # to guess from, so renewal would leave the Connector alone.
    connector.oauth_metadata = stored
    connector.connected = True
    # A fresh grant is exactly what the flag was asking for.
    connector.oauth_reauthorization_required = False
    await db.commit()
    return connector


async def release(redis: aioredis.Redis, connector: MCPServer, *, defer: Defer) -> None:
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

    ``defer`` runs the provider revocation after the response — the caller's
    ``BackgroundTasks.add_task`` — so this module stays free of the web layer.

    What it deliberately does not touch is how to obtain the *next* one — see
    :func:`forget_how_to_authorize`, which only the conversion wants.
    """
    _schedule_revocation(connector, defer)
    grant.discard(connector)
    # Deleting the pointer is what says "waiting for nothing", and
    # :func:`_reject_an_unawaited_authorization` reads that as a refusal: a
    # flow the user started earlier keeps its own ``state`` for the whole TTL,
    # and would otherwise come back, write a grant and set ``connected`` again.
    # The pointer rather than the pending records, because it covers every
    # flow in the air at once — two tabs open means two live ``state`` values.
    await redis.delete(oauth_current_state_key(connector.id))


def forget_how_to_authorize(connector: MCPServer) -> None:
    """Drop what the row needs to obtain an authorization at all.

    The client registration, the scope and the discovery metadata: not the
    authorization but the standing to ask for one. Kept across a disconnect,
    which is what makes reconnecting a single browser round-trip; dropped when
    the Connector stops being an OAuth Connector, because then nothing can reach
    them again.

    The metadata is the one that bites. It is what a renewal aims its rotation
    at, so a row that keeps it is a row renewal would go on working after the
    user replaced its credential with a static one.

    Together with :func:`release` this is every ``oauth_*`` column, which is
    the pair a conversion calls — and the reason each has a docstring saying
    which half it owns, so a column added later has one obvious home rather
    than two plausible ones.
    """
    connector.oauth_client_encrypted = None
    connector.oauth_scope = None
    connector.oauth_metadata = None


async def register_client(
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
    redirect = redirect_uri()
    discovered = await oauth.discover_authorization(url)
    client = await oauth.ensure_client(
        discovered.metadata,
        redirect_uri=redirect,
        scope=discovered.scope_for(scope),
        client_id=client_id,
        client_secret=client_secret,
        client_metadata_url=settings.mcp_client_metadata_url or None,
        client_name=settings.mcp_client_name,
    )
    return encrypt_secret(client.model_dump_json()), discovered.as_stored()


def redirect_uri() -> str:
    """The redirect URI every OAuth step needs, or a loud refusal.

    Logged here rather than left to the handler, which serializes an
    :class:`AppError` without a line of its own: this is the deployment's own
    omission, nobody downstream can act on it, and a 500 with nothing in the
    log is how it stays unnoticed.
    """
    if not settings.mcp_oauth_redirect_uri:
        logger.error("mcp_oauth_redirect_uri is not set; no Connector can use OAuth")
        raise OAuthNotConfiguredError("OAuth is not configured on this server.")
    return settings.mcp_oauth_redirect_uri


def _schedule_revocation(connector: MCPServer, defer: Defer) -> None:
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
    inside :func:`release` is not incidental.

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
            "cannot be told it is finished with",
            connector.id,
            exc_info=True,
        )
        return
    if tokens is None or authorization is None:
        return
    metadata, client = authorization
    if metadata.revocation_endpoint is None:
        logger.info(
            "Connector %s: its authorization server offers no revocation "
            "endpoint; dropping our copy of the grant only",
            connector.id,
        )
        return
    defer(
        _revoke_quietly,
        connector.id,
        metadata,
        client,
        tokens,
    )


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
            connector_id,
            exc_info=True,
        )


def _stored_client(connector: MCPServer) -> OAuthClientInformationFull:
    """The row's OAuth client, or a clean failure when it has none.

    A well-formed OAuth connector always has one (create registers it and PATCH
    can't switch a connector to OAuth), so a missing one means a malformed row.
    """
    client = grant.stored_client(connector)
    if client is None:
        raise ConnectorOAuthError("connector has no OAuth client registration; re-create it.")
    return client


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
        raise ConnectorNotFoundError("Connector not found.")
    if connector.oauth_client_encrypted != authorizing_client:
        raise ConnectorChangedDuringAuthorizationError(
            "this connector's credentials changed while you were authorizing; start again"
        )
    await _reject_an_unawaited_authorization(redis, state, pending)
    return connector


async def _consume_pending_authorization(
    redis: aioredis.Redis,
    state: str,
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
    redis: aioredis.Redis,
    state: str,
    pending: dict,
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
    :func:`start` writes both with the same TTL, the pending one first, so it
    is always the first to expire.

    Every ending reaches this, because every ending goes through
    :func:`release`. Two of them would also be caught further on — a
    deleted Connector 404s, a converted one moves the ciphertext
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
        raise ConnectorAuthorizationExpiredError(
            "this connector is no longer waiting to be authorized; connect it again to start over"
        )
    raise ConnectorAuthorizationSupersededError(
        "a newer authorization was started for this connector; finish that one instead"
    )


async def _client_for_authorization(
    db: AsyncSession,
    connector: MCPServer,
    discovered: oauth.DiscoveredAuthorization,
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
    client = _stored_client(connector)
    unusable = _why_the_stored_client_is_unusable(connector, client, discovered)
    if unusable is None:
        return client
    if not _issued_by_an_authorization_server(client):
        raise ConnectorOAuthClientUnusableError(f"{unusable}; create a new OAuth connector")
    logger.info("Connector %s: %s; registering again", connector.id, unusable)
    # A refresh token is bound to the client it was issued to (RFC 6749 §6),
    # so the grant cannot outlive the client it is about to lose. Left
    # standing it would also be renewable before the browser leg finishes —
    # this commits first — and a renewal would present it, beside the new
    # client, to whichever token endpoint the row now names.
    grant.discard(connector)
    fresh = await oauth.ensure_client(
        discovered.metadata,
        redirect_uri=redirect_uri(),
        scope=discovered.scope_for(connector.oauth_scope),
        client_metadata_url=settings.mcp_client_metadata_url or None,
        client_name=settings.mcp_client_name,
    )
    connector.oauth_client_encrypted = encrypt_secret(fresh.model_dump_json())
    # Stored with it: a client belongs to the server that issued it, and for a
    # row that had no issuer recorded at all, this reconnect is what gives it
    # one. Without this the row would arrive here again every time.
    connector.oauth_metadata = discovered.as_stored()
    await db.commit()
    return fresh


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
    if not _registered_issuer(connector):
        if not client.client_secret:
            return None
        return "this connector predates authorization-server binding"
    if _authorization_server_changed(connector, discovered):
        return "this connector's authorization server changed"
    return None


def _registered_issuer(connector: MCPServer) -> str | None:
    """The authorization server this row's client and grant both came from.

    They are obtained in one flow against one server and committed together, so
    a single record dates both — which is what lets a caller ask whether either
    still belongs where the row now points.
    """
    return (connector.oauth_metadata or {}).get("authorization_server", {}).get("issuer")


def _authorization_server_changed(
    connector: MCPServer,
    discovered: oauth.DiscoveredAuthorization,
) -> bool:
    """Whether the MCP server now names a different authorization server.

    ``False`` when the row records none: an absent issuer is evidence of
    nothing, and callers act on this only where positive evidence is required.

    A trailing slash is not a different server, and saying so is load-bearing
    rather than tidy. Rows written under mcp 1.x hold the issuer as *that* SDK
    rendered it, which gave a bare origin a slash; 2.x records what the document
    published. Compared exactly, every OAuth Connector whose issuer is a bare
    origin would read as repointed the first time it was re-examined after the
    upgrade — and be stripped of the client registration this function guards.

    Deliberately not the comparison :func:`~app.services.mcp.oauth.require_matching_issuer`
    makes. That one answers "did this authorization *response* come from the
    server we started with", where RFC 9207 forbids normalising anything. This
    one answers "is the row's grant still about the server the document names",
    and two spellings of one origin are one server.
    """
    registered_issuer = _registered_issuer(connector)
    if not registered_issuer:
        return False
    return str(discovered.metadata.issuer).rstrip("/") != registered_issuer.rstrip("/")


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
    return client.client_id_issued_at is not None or client.client_secret_expires_at is not None


def _secret_expired(client: OAuthClientInformationFull) -> bool:
    """Whether the client's own credential has passed its stated expiry.

    ``0`` means "never expires" per RFC 7591 §3.2.1, and absent means the same;
    only a non-zero value is a deadline.
    """
    expires_at = client.client_secret_expires_at
    # ``is not None`` and ``!= 0`` spelled out: ``bool(...)`` covers both at
    # runtime but tells a reader (and a checker) neither.
    return expires_at is not None and expires_at != 0 and expires_at <= int(time.time())
