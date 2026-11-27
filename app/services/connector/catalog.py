"""The official Connector catalog: its entries, and how one becomes a
user's Connector.

Two sides of ``mcp_server_catalog``. The customer surface reads what is
published and instantiates an entry. The console and the operator script
manage the entries through :func:`create_entry` and :func:`update_entry`,
which hold the rules an entry keeps whoever writes it — one entry per URL,
a category that exists, no locale map or OAuth scope outliving the thing
it described. Neither commits: the console's audit owns its transaction,
and the script its own.

Instantiating **copies** the template onto a new user-owned ``mcp_server`` row
rather than referencing it live. That is what keeps a Connector working after
its catalog entry is edited or retired, and what lets every existing per-user
path (authorize, the worker's grant renewal, the ``connected`` toggle, delete)
stay untouched.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    CategoryNotFoundError,
    ConnectorCatalogEntryNotFoundError,
    ConnectorCatalogUrlTakenError,
)
from app.models.connector_category import ConnectorCategory
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.services import localized_text

#: The auth types an official entry can actually be added with. BEARER/HEADER
#: are excluded on purpose: they need a secret that is per-user by nature, and
#: the catalog holds none — see :func:`build_connector`. Operator tooling should
#: offer only these.
INSTANTIABLE_AUTH_TYPES = frozenset({MCPServerAuthType.NONE, MCPServerAuthType.OAUTH})

#: Each canonical column beside the locale map that shadows it. Editing
#: one without clearing the other is the bug this pairing exists to make
#: hard to reintroduce — see :func:`update_entry`.
_LOCALIZED_FIELDS = (("name", "name_i18n"), ("description", "description_i18n"))


async def create_entry(
    db: AsyncSession,
    *,
    name: str,
    url: str,
    auth_type: MCPServerAuthType,
    description: str | None = None,
    icon_url: str | None = None,
    oauth_scope: str | None = None,
    published: bool = False,
    sort_order: int = 0,
    category_id: int | None = None,
    name_i18n: dict[str, str] | None = None,
    description_i18n: dict[str, str] | None = None,
) -> MCPServerCatalogEntry:
    """Add a catalog entry and flush it, so it has its id.

    Unpublished unless said otherwise, so an operator can check the URL and
    icon render before users see it. Raises
    :class:`ConnectorCatalogUrlTakenError` for a URL the catalog already
    offers and :class:`CategoryNotFoundError` for a category it does not
    hold.
    """
    await _assert_url_free(db, url)
    await _assert_category_exists(db, category_id)
    entry = MCPServerCatalogEntry(
        name=name,
        name_i18n=name_i18n,
        url=url,
        auth_type=auth_type,
        description=description,
        description_i18n=description_i18n,
        icon_url=icon_url,
        oauth_scope=oauth_scope,
        published=published,
        sort_order=sort_order,
        category_id=category_id,
    )
    db.add(entry)
    await db.flush()
    return entry


async def update_entry(
    db: AsyncSession, entry: MCPServerCatalogEntry, fields: Mapping[str, Any]
) -> dict[str, Any]:
    """Write *fields* onto *entry*, and clear what the edit leaves stale.

    Returns the columns it cleared that the caller did not name, each with
    the value it held — the only copy left of it, which is why the console
    puts them in its audit payload.

    Editing the URL can collide: the URL is the entry's identity and it is
    editable, so this checks the same thing :func:`create_entry` does.

    **A scope is meaningless without OAuth**, and a caller's update cannot
    express "clear this" — ``None`` there means "leave alone" — so the
    invariant is kept here rather than asked of the caller. Left behind it
    would reappear the day someone switches the entry back to OAUTH, and be
    copied onto every Connector made from it in between.

    **Clearing a locale map is not optional** — see the invalidation rule
    in :mod:`app.services.localized_text`. Left behind it would be
    invisible: the public catalog would keep serving the old text,
    :func:`build_connector` would copy it onto every Connector created
    afterwards, and the console shows the canonical column, so the operator
    would see the edit take effect. Cleared whole rather than per-locale
    because the console can neither show nor edit these maps: a translation
    of the superseded text would be a divergence an operator has no way to
    discover.
    """
    url = fields.get("url")
    if url is not None and url != entry.url:
        await _assert_url_free(db, url)
    await _assert_category_exists(db, fields.get("category_id"))

    # Read before the writes: what the entry loses is its value from before
    # this edit, not one the edit itself carried.
    previous_scope = entry.oauth_scope
    reworded = [
        locale_map
        for column, locale_map in _LOCALIZED_FIELDS
        if column in fields and fields[column] != getattr(entry, column)
    ]

    for column, value in fields.items():
        setattr(entry, column, value)

    cleared: dict[str, Any] = {}
    if entry.auth_type is not MCPServerAuthType.OAUTH and entry.oauth_scope:
        cleared["oauth_scope"] = previous_scope
        entry.oauth_scope = None
    for locale_map in reworded:
        stale = getattr(entry, locale_map)
        if stale is not None:
            cleared[locale_map] = stale
            setattr(entry, locale_map, None)
    return cleared


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


def build_connector(entry: MCPServerCatalogEntry, user_id: int, locale: str) -> MCPServer:
    """An unsaved user-owned Connector copied from *entry*.

    The name is copied in *locale* — whichever the user was reading when they
    added it. The copy is a snapshot from then on: the row is theirs, free to
    rename, and never re-derived from the catalog, so switching the UI
    language later leaves it alone. Copying the canonical name instead would
    hand an English reader a Chinese one for a Connector they added from an
    English card.

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
        name=localized_text.resolve(entry.name, entry.name_i18n, locale),
        url=entry.url,
        auth_type=entry.auth_type,
        oauth_scope=entry.oauth_scope,
        # OAUTH starts inactive until authorized — same rule as a hand-added
        # OAuth Connector.
        connected=entry.auth_type != MCPServerAuthType.OAUTH,
        tool_count=0,
    )


async def _assert_url_free(db: AsyncSession, url: str) -> None:
    """Refuse a URL the catalog already offers.

    Checked here rather than left to the unique constraint so the operator
    gets a named 409 instead of an ``IntegrityError`` surfacing as a 500.
    The constraint is still the thing that makes it true under a race; this
    is the readable path.
    """
    if await db.scalar(select(MCPServerCatalogEntry.id).where(MCPServerCatalogEntry.url == url)):
        raise ConnectorCatalogUrlTakenError(f"The catalog already offers {url}.")


async def _assert_category_exists(db: AsyncSession, category_id: int | None) -> None:
    """Refuse a category the taxonomy does not hold.

    The foreign key makes it true; this makes it readable, and raises
    before anything is written.

    ``None`` is the uncategorised state, not a missing row, and passes.
    """
    if category_id is not None and await db.get(ConnectorCategory, category_id) is None:
        raise CategoryNotFoundError(f"No connector category with id {category_id}.")
