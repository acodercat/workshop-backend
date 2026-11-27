"""The console's Connector surface: the platform catalog, and a
read-only view of what users have configured.

- ``GET   /connector-catalog``       — every entry, published or not.
- ``POST  /connector-catalog``       — add one, unpublished by default.
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
    ConnectorCatalogEntryNotFoundError,
)
from app.models.mcp_server import MCPServer
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.models.user import User
from app.schemas.admin.connector import (
    ConnectorCatalogEntryCreate,
    ConnectorCatalogEntryResponse,
    ConnectorCatalogEntryUpdate,
    ConnectorResponse,
)
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.services.admin import AuditAction, audit, field_changes
from app.services.connector import catalog

router = APIRouter(tags=["connectors"])


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
    async with audit(
        ctx,
        action=AuditAction.CONNECTOR_CATALOG_CREATE,
        target_type="connector_catalog",
    ) as draft:
        entry = await catalog.create_entry(ctx.db, **body.model_dump())
        draft.target_id = entry.id
        draft.payload = {
            "name": entry.name,
            "url": entry.url,
            "published": entry.published,
        }

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

    The rules an edit keeps — a free URL, a real category, nothing stale
    left behind — are :func:`~app.services.connector.catalog.update_entry`'s.
    """
    entry = await ctx.db.get(MCPServerCatalogEntry, entry_id)
    if entry is None:
        raise ConnectorCatalogEntryNotFoundError(f"No catalog entry with id {entry_id}.")

    fields = body.model_dump(exclude_none=True)
    # Put back the one field whose explicit ``null`` is an instruction
    # rather than a default — see
    # ``ConnectorCatalogEntryUpdate.category_id``.
    if "category_id" in body.model_fields_set:
        fields["category_id"] = body.category_id

    changes = field_changes(entry, fields)
    if not changes:
        return _to_catalog_response(entry)

    async with audit(
        ctx,
        action=AuditAction.CONNECTOR_CATALOG_UPDATE,
        target_type="connector_catalog",
        target_id=entry.id,
    ) as draft:
        cleared = await catalog.update_entry(ctx.db, entry, fields)
        # A scope the edit cleared is a change to a column the request may
        # also have named, so it is recorded as one, from the value the
        # entry held before.
        if "oauth_scope" in cleared:
            changes["oauth_scope"] = {"from": cleared.pop("oauth_scope"), "to": None}
        # ``entry_name`` rather than ``name``: unlike a skill's, this
        # name is editable, so under one key the label and the
        # before → after entry would overwrite each other and the key
        # would hold a string on some rows and a diff on others.
        #
        # A discarded locale map goes in whole, since unlike a skill's
        # (which its bundle re-supplies) this one is hand-curated and the
        # trail is the only copy left.
        draft.payload = {
            "entry_name": entry.name,
            **changes,
            **{f"cleared_{column}": value for column, value in cleared.items()},
        }

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
