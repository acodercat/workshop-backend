"""How an OAuth grant is written onto a Connector row.

Two callers persist one: the authorization callback, which has just exchanged a
code, and the token refresher, which has just rotated. Anything they disagreed
about would be a Connector that works right up until its first refresh, so the
rule lives here once rather than at each of them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp.shared.auth import OAuthToken

from app.models.mcp_server import MCPServer
from app.services.crypto import decrypt_secret, encrypt_secret

# What to assume a token lives for when its issuer would not say. An hour is
# the common default across providers, and being wrong only means refreshing
# sooner than needed.
ASSUMED_TOKEN_LIFETIME = 3600


def store_tokens(
    connector: MCPServer, tokens: OAuthToken, *, rotation: bool,
) -> None:
    """Persist a grant together with when it stops being valid.

    The two always move together: ``expires_in`` counts from *now*, so the
    moment the grant is written is the only place its absolute deadline is
    knowable. Storing the ciphertext without it would leave nothing able to
    tell an expired token from a fresh one.

    *rotation* says which of the two callers this is, and it is not a detail.
    A refresh response may omit a refresh token and mean "keep using yours"; an
    authorization-code response means "here is the whole new grant", and
    inheriting the old grant's refresh token there would carry a credential
    across a re-authorization — including one the user made against a *different*
    authorization server.
    """
    stored = _carrying_refresh_token(connector, tokens) if rotation else tokens
    connector.oauth_tokens_encrypted = encrypt_secret(stored.model_dump_json())
    connector.oauth_tokens_expires_at = _deadline_for(stored)


def _deadline_for(tokens: OAuthToken) -> datetime | None:
    """When to consider *tokens* spent — or ``None`` when nothing could act on it.

    ``expires_in`` is "the lifetime in seconds of the access token"
    (RFC 6749 §4.2.2), so ``0`` is a server stating that its token is already
    spent — not a server declining to say. Only ``None`` is silence, and the
    difference matters: reading ``0`` as unstated would give the row an hour it
    does not have, and every turn in that hour would send a token known to be
    dead.

    Clamped at zero all the same. The SDK's model does not police the value, and
    a negative would put this deadline decades in the past — which is not merely
    early but unrecoverable, since the refresher orders by this column and such a
    row is then the most overdue thing in the queue forever. Already-expired is
    the most a server can truthfully say; the magnitude below zero carries no
    further meaning to honour.

    A server that states nothing gets :data:`ASSUMED_TOKEN_LIFETIME` — but only
    while a refresh token exists to act on it with. Some servers issue neither
    (Notion's MCP OAuth and Salesforce are both reported to), and assuming a
    deadline for one of those buys nothing and costs a false alarm: the
    refresher would pick the row up an hour later, find nothing to rotate with,
    and ask the user to re-authorize a token that had not expired at all —
    every hour, forever. ``None`` keeps such a grant out of the refresher's
    query entirely and lets it be used until the server itself refuses it.
    """
    if tokens.expires_in is not None:
        return datetime.now(timezone.utc) + timedelta(
            seconds=max(tokens.expires_in, 0)
        )
    if not tokens.refresh_token:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=ASSUMED_TOKEN_LIFETIME)


def stored_tokens(connector: MCPServer) -> OAuthToken | None:
    """The grant currently on the row, decrypted."""
    if not connector.oauth_tokens_encrypted:
        return None
    return OAuthToken.model_validate_json(
        decrypt_secret(connector.oauth_tokens_encrypted)
    )


def _carrying_refresh_token(connector: MCPServer, tokens: OAuthToken) -> OAuthToken:
    """*tokens*, keeping the stored refresh token when the response omits one.

    A refresh response need not reissue one (OAuth 2.1 §4.3.2) — the existing
    one stays valid, and only a reissue replaces it. Writing the response
    verbatim would throw away a refresh token the provider never revoked, and
    turn the next expiry into a browser round-trip.

    Only ever on that path. A code exchange establishes a new grant, so an old
    refresh token has no standing in it — and carrying one forward would hand
    it to whichever authorization server the *new* grant came from.
    """
    if tokens.refresh_token is not None:
        return tokens
    previous = stored_tokens(connector)
    if previous is None or previous.refresh_token is None:
        return tokens
    return tokens.model_copy(update={"refresh_token": previous.refresh_token})
