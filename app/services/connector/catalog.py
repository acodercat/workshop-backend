"""The official Connector catalog: read the catalog, instantiate an entry.

The customer surface's view of ``mcp_server_catalog``: what is published,
and how an entry becomes a user's Connector. The console and the operator
script manage the entries themselves.

Instantiating **copies** the template onto a new user-owned ``mcp_server`` row
rather than referencing it live. That is what keeps a Connector working after
its catalog entry is edited or retired, and what lets every existing per-user
path (authorize, the worker's grant renewal, the ``connected`` toggle, delete)
stay untouched.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    ConnectorCatalogCredentialError,
    ConnectorCatalogEntryNotFoundError,
)
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.services import localized_text

#: The auth types an official entry can be added with. HEADER is excluded: its
#: header *name* is template-level and the catalog has no column for it.
INSTANTIABLE_AUTH_TYPES = frozenset(
    {MCPServerAuthType.NONE, MCPServerAuthType.OAUTH, MCPServerAuthType.BEARER}
)

#: The instantiable types whose entry needs a secret the *user* supplies at add
#: time. The catalog itself still holds none — see :func:`build_connector`.
CREDENTIAL_AUTH_TYPES = frozenset({MCPServerAuthType.BEARER})


async def list_published(db: AsyncSession) -> list[MCPServerCatalogEntry]:
    """Every entry offered in the catalog, in display order.

    Unpublished entries are invisible here but keep their rows, so Connectors
    already instantiated from them retain their ``catalog_id``.
    """
    stmt = (
        select(MCPServerCatalogEntry)
        .where(MCPServerCatalogEntry.published.is_(True))
        .order_by(MCPServerCatalogEntry.sort_order, MCPServerCatalogEntry.id)
    )
    result = await db.execute(stmt)
    return list(result.scalars())


async def get_published_entry(
    db: AsyncSession,
    catalog_id: int,
) -> MCPServerCatalogEntry:
    """One entry the caller is allowed to add.

    Unpublished reads as missing on purpose: an unlisted entry must not be
    addable by id just because the id leaked from an earlier listing.
    """
    stmt = select(MCPServerCatalogEntry).where(
        MCPServerCatalogEntry.id == catalog_id,
        MCPServerCatalogEntry.published.is_(True),
    )
    result = await db.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry is None:
        raise ConnectorCatalogEntryNotFoundError(f"catalog entry {catalog_id} is not available")
    return entry


def build_connector(
    entry: MCPServerCatalogEntry,
    user_id: int,
    locale: str,
    *,
    encrypted_credential: str | None = None,
) -> MCPServer:
    """An unsaved user-owned Connector copied from *entry*.

    The name is copied in *locale* — whichever the user was reading when they
    added it. The copy is a snapshot from then on: the row is theirs, free to
    rename, and never re-derived from the catalog, so switching the UI
    language later leaves it alone. Copying the canonical name instead would
    hand an English reader a Chinese one for a Connector they added from an
    English card.

    The catalog never holds a per-user secret. OAUTH entries get theirs from
    the authorize flow; a :data:`CREDENTIAL_AUTH_TYPES` entry (BEARER) gets
    the user's own token here, as *encrypted_credential* supplied by the add
    request — required for such an entry and refused for any other, so a
    mismatch fails at add time rather than 401ing on every tool call. What an
    entry *may* carry is the platform's own pre-registered OAuth app
    (``oauth_client_id`` + encrypted secret, for providers without DCR/CIMD) —
    one app shared by every user, not a credential, applied by the add
    endpoint at client registration rather than copied here.
    """
    if entry.auth_type not in INSTANTIABLE_AUTH_TYPES:
        raise ConnectorCatalogEntryNotFoundError(
            f"catalog entry {entry.id} declares {entry.auth_type.value} auth, "
            f"which the catalog cannot offer"
        )
    takes_credential = entry.auth_type in CREDENTIAL_AUTH_TYPES
    if takes_credential and encrypted_credential is None:
        raise ConnectorCatalogCredentialError(
            f"catalog entry {entry.id} requires your {entry.auth_type.value} credential to be added"
        )
    if not takes_credential and encrypted_credential is not None:
        raise ConnectorCatalogCredentialError(f"catalog entry {entry.id} takes no credential")
    return MCPServer(
        user_id=user_id,
        catalog_id=entry.id,
        name=localized_text.resolve(entry.name, entry.name_i18n, locale),
        url=entry.url,
        auth_type=entry.auth_type,
        oauth_scope=entry.oauth_scope,
        encrypted_credential=encrypted_credential,
        # OAUTH starts inactive until authorized — same rule as a hand-added
        # OAuth Connector.
        connected=entry.auth_type != MCPServerAuthType.OAUTH,
        tool_count=0,
    )
