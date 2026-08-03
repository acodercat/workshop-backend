"""The official Connector catalog: read the catalog, instantiate an entry.

The only module that reads ``mcp_server_catalog``. Endpoints and the operator
script both go through here, so adding an admin CRUD later means adding a
router — not re-deriving these rules.

Instantiating **copies** the template onto a new user-owned ``mcp_server`` row
rather than referencing it live. That is what keeps a Connector working after
its catalog entry is edited or retired, and what lets every existing per-user
path (authorize, the worker's grant renewal, the ``connected`` toggle, delete)
stay untouched.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConnectorCatalogEntryNotFoundError
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry

#: The auth types an official entry can actually be added with. BEARER/HEADER
#: are excluded on purpose: they need a secret that is per-user by nature, and
#: the catalog holds none — see :func:`build_connector`. Operator tooling should
#: offer only these.
INSTANTIABLE_AUTH_TYPES = frozenset(
    {MCPServerAuthType.NONE, MCPServerAuthType.OAUTH}
)


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
    db: AsyncSession, catalog_id: int,
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
        raise ConnectorCatalogEntryNotFoundError(
            f"catalog entry {catalog_id} is not available"
        )
    return entry


def build_connector(entry: MCPServerCatalogEntry, user_id: int) -> MCPServer:
    """An unsaved user-owned Connector copied from *entry*.

    Per-user credentials are deliberately absent — OAUTH entries get theirs
    from the authorize flow. That is why only
    :data:`INSTANTIABLE_AUTH_TYPES` can be offered: BEARER/HEADER need a
    per-user secret nobody here can supply, so the Connector would probe fine
    and then 401 on every real tool call. What an entry *may* carry is the
    platform's own pre-registered OAuth app (``oauth_client_id`` + encrypted
    secret, for providers without DCR/CIMD) — that is one app shared by every
    user, not a credential, and the add endpoint applies it at client
    registration rather than copying it here.
    """
    if entry.auth_type not in INSTANTIABLE_AUTH_TYPES:
        raise ConnectorCatalogEntryNotFoundError(
            f"catalog entry {entry.id} declares {entry.auth_type.value} auth, "
            f"which cannot be added without a per-user credential"
        )
    return MCPServer(
        user_id=user_id,
        catalog_id=entry.id,
        name=entry.name,
        url=entry.url,
        auth_type=entry.auth_type,
        oauth_scope=entry.oauth_scope,
        # OAUTH starts inactive until authorized — same rule as a hand-added
        # OAuth Connector.
        connected=entry.auth_type != MCPServerAuthType.OAUTH,
        tool_count=0,
    )
