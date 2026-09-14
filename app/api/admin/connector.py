"""The console's Connector surface: the platform catalog, and a
read-only view of what users have configured.

- ``GET   /connector-catalog``       — every entry, published or not.
- ``POST  /connector-catalog``       — publish a new one.
- ``PATCH /connector-catalog/{id}``  — edit / publish / reorder.
- ``GET   /connectors``              — users' own Connectors, read-only.

Replaces day-to-day use of ``scripts/manage_connector_catalog.py``. What
stays in that script is seeding a **preregistered OAuth client**, because
that means sealing a client secret — keeping it there is what lets this
surface handle no secrets at all (see
:mod:`app.schemas.admin.connector`).

The user-Connector view is deliberately read-only. A user's Connector is
their configuration; the console reads it to answer "why is this
failing", it does not reach into it.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.common.pagination import paginate
from app.errors import (
    CategoryNotFoundError,
    ConnectorCatalogEntryNotFoundError,
    ConnectorCatalogUrlTakenError,
)
from app.models.connector_category import ConnectorCategory
from app.models.mcp_server import MCPServer, MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.models.user import User
from app.schemas.admin.connector import (
    ConnectorCatalogEntryCreate,
    ConnectorCatalogEntryResponse,
    ConnectorCatalogEntryUpdate,
    ConnectorResponse,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta

router = APIRouter(tags=["connectors"])

#: Each canonical column beside the locale map that shadows it. Editing
#: one without clearing the other is the bug this pairing exists to make
#: hard to reintroduce — see ``update_catalog_entry``.
_LOCALIZED_FIELDS = (("name", "name_i18n"), ("description", "description_i18n"))


def _to_catalog_response(
    entry: MCPServerCatalogEntry,
) -> ConnectorCatalogEntryResponse:
    return ConnectorCatalogEntryResponse(
        id=entry.id,
        name=entry.name,
        url=entry.url,
        auth_type=entry.auth_type,
        description=entry.description,
        icon_url=entry.icon_url,
        oauth_scope=entry.oauth_scope,
        # Whether, never what: enough to explain why a connector skips
        # dynamic registration, with nothing of the secret itself.
        has_preregistered_client=entry.oauth_client_id is not None,
        published=entry.published,
        sort_order=entry.sort_order,
        category_id=entry.category_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _to_connector_response(
    connector: MCPServer, owner_username: str, owner_name: str
) -> ConnectorResponse:
    return ConnectorResponse(
        id=connector.id,
        user_id=connector.user_id,
        owner_username=owner_username,
        owner_name=owner_name,
        name=connector.name,
        url=connector.url,
        auth_type=connector.auth_type,
        connected=connector.connected,
        last_connect_failed=connector.last_connect_failed,
        oauth_reauthorization_required=connector.oauth_reauthorization_required,
        tool_count=connector.tool_count,
        disabled_tool_count=len(connector.disabled_tools or []),
        catalog_id=connector.catalog_id,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


async def _enforce_unique_url(ctx: StaffContext, url: str) -> None:
    """Refuse a URL the catalog already offers.

    Checked here rather than left to the unique constraint so the
    operator gets a named 409 instead of an IntegrityError surfacing as
    a 500. The constraint is still the thing that makes it true under a
    race; this is the readable path.
    """
    if await ctx.db.scalar(
        select(MCPServerCatalogEntry.id).where(MCPServerCatalogEntry.url == url)
    ):
        raise ConnectorCatalogUrlTakenError(f"The catalog already offers {url}.")


async def _enforce_category_exists(ctx: StaffContext, category_id: int | None) -> None:
    """Refuse a category the taxonomy does not hold.

    Checked here for the reason :func:`_enforce_unique_url` is: the
    foreign key makes it true, this makes it readable.

    ``None`` is the uncategorised state, not a missing row, and passes.
    """
    if category_id is not None and await ctx.db.get(ConnectorCategory, category_id) is None:
        raise CategoryNotFoundError(f"No connector category with id {category_id}.")


@router.get(
    "/connector-catalog",
    response_model=list[ConnectorCatalogEntryResponse],
)
async def list_catalog(ctx: StaffContext) -> list[ConnectorCatalogEntryResponse]:
    """Return the whole catalog in display order, unpublished included.

    Not paginated: the catalog is a curated shortlist an operator orders
    by hand, and paging a sortable list would hide the thing being
    sorted.
    """
    rows = (
        await ctx.db.scalars(
            select(MCPServerCatalogEntry).order_by(
                MCPServerCatalogEntry.sort_order, MCPServerCatalogEntry.name
            )
        )
    ).all()
    return [_to_catalog_response(entry) for entry in rows]


@router.post(
    "/connector-catalog",
    response_model=ConnectorCatalogEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_catalog_entry(
    ctx: StaffContext, body: ConnectorCatalogEntryCreate
) -> ConnectorCatalogEntryResponse:
    """Add a catalog entry.

    Created unpublished by default so an operator can check the URL and
    icon render before users see it — publishing is the separate PATCH.
    """
    await _enforce_unique_url(ctx, body.url)
    await _enforce_category_exists(ctx, body.category_id)

    entry = MCPServerCatalogEntry(
        name=body.name,
        url=body.url,
        auth_type=body.auth_type,
        description=body.description,
        icon_url=body.icon_url,
        oauth_scope=body.oauth_scope,
        published=body.published,
        sort_order=body.sort_order,
        category_id=body.category_id,
    )
    ctx.db.add(entry)
    await ctx.db.commit()

    await ctx.db.refresh(entry)
    return _to_catalog_response(entry)


@router.patch(
    "/connector-catalog/{entry_id}",
    response_model=ConnectorCatalogEntryResponse,
)
async def update_catalog_entry(
    ctx: StaffContext, entry_id: int, body: ConnectorCatalogEntryUpdate
) -> ConnectorCatalogEntryResponse:
    """Edit, publish/unpublish, or reorder a catalog entry.

    Editing the URL can collide, which is new: while ``key`` carried the
    unique constraint it was immutable, so no edit could conflict. Now
    the identity is the URL and the URL is editable, so this checks the
    same thing the create path does.
    """
    entry = await ctx.db.get(MCPServerCatalogEntry, entry_id)
    if entry is None:
        raise ConnectorCatalogEntryNotFoundError(f"No catalog entry with id {entry_id}.")

    if body.url is not None and body.url != entry.url:
        await _enforce_unique_url(ctx, body.url)

    fields = body.model_dump(exclude_none=True)
    # Put back the one field whose explicit ``null`` is an instruction
    # rather than a default — see
    # ``ConnectorCatalogEntryUpdate.category_id``.
    if "category_id" in body.model_fields_set:
        fields["category_id"] = body.category_id

    await _enforce_category_exists(ctx, fields.get("category_id"))

    changed = {field for field, value in fields.items() if getattr(entry, field) != value}
    if not changed:
        return _to_catalog_response(entry)

    for field in changed:
        setattr(entry, field, fields[field])

    # A scope is meaningless without OAuth, and the update schema
    # cannot express "clear this" — ``None`` there means "leave
    # alone" — so the invariant is kept here rather than asked of
    # the caller. Left behind it would reappear the day someone
    # switches the entry back to OAUTH, and be copied onto every
    # Connector made from it in between.
    if entry.auth_type is not MCPServerAuthType.OAUTH and entry.oauth_scope:
        entry.oauth_scope = None

    # Clearing a locale map is not optional — see the invalidation
    # rule in ``app.services.localized_text``. Left behind here it
    # would be invisible: the public catalog would keep serving the
    # old text, ``build_connector`` would copy it onto every
    # Connector created afterwards, and this endpoint returns the
    # canonical column, so the operator would see the edit take
    # effect.
    #
    # Cleared whole rather than per-locale because the console can
    # neither show nor edit these maps: a translation of the
    # superseded text would be a divergence an operator has no way
    # to discover. This deployment keeps no operator trail, so the
    # discarded map — hand-curated, unlike a skill's, which its bundle
    # re-supplies — is gone for good once cleared.
    for field, locale_map in _LOCALIZED_FIELDS:
        if field in changed:
            setattr(entry, locale_map, None)

    await ctx.db.commit()

    await ctx.db.refresh(entry)
    return _to_catalog_response(entry)


@router.get("/connectors", response_model=PaginatedResponse[ConnectorResponse])
async def list_connectors(
    ctx: StaffContext,
    user_id: int | None = Query(None),
    q: str | None = Query(None, min_length=1, max_length=64),
    connected: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[ConnectorResponse]:
    """Return one page of users' Connectors, newest first.

    ``?connected=false`` is the support view — the Connectors that
    stopped working — which is why the filter exists at all.
    """
    filters = []
    if user_id is not None:
        filters.append(MCPServer.user_id == user_id)
    if connected is not None:
        filters.append(MCPServer.connected.is_(connected))
    if q is not None:
        pattern = f"%{q}%"
        filters.append(sa.or_(MCPServer.name.ilike(pattern), MCPServer.url.ilike(pattern)))

    total = await ctx.db.scalar(select(func.count()).select_from(MCPServer).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(MCPServer, User.username, User.name)
                .join(User, User.id == MCPServer.user_id)
                .where(*filters)
                .order_by(MCPServer.created_at.desc(), MCPServer.id.desc()),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[
            _to_connector_response(connector, username, name) for connector, username, name in rows
        ],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )
