"""The console's two category taxonomies — the tabs on the skills and
connectors pages.

- ``GET   /skill-categories``           ``GET   /connector-categories``
- ``POST  /skill-categories``           ``POST  /connector-categories``
- ``PATCH /skill-categories/{id}``      ``PATCH /connector-categories/{id}``

Two route groups in one module because they are one screen and one
shape; the tables stay separate so a query cannot reach across them (see
:mod:`app.models.skill_category`). What differs between the groups is a
model, a target type and two audit actions — carried in
:class:`_Taxonomy` so the body of each verb is written once. Anything
that would need a second knob added there belongs in its own module
instead.

**No DELETE**, matching the catalog surface: unpublish hides a tab
without touching the rows pointing at it. Deleting is in fact safe here
— both foreign keys are ``ON DELETE SET NULL`` — but it would quietly
turn N entries uncategorised with nothing left to say which they were.

Assigning a category to a skill or a catalog entry is not here: it is a
field on those resources' own PATCH, and lands in their existing audit
rows, which is where someone asking "who moved this" will look.
"""

from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.admin.dependencies import StaffContext
from app.errors import CategoryNotFoundError
from app.models.connector_category import ConnectorCategory
from app.models.skill_category import SkillCategory
from app.schemas.admin.category import (
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
)
from app.services import localized_text
from app.services.admin import AuditAction, audit

router = APIRouter(tags=["categories"])

#: Matches ``String(64)`` on both tables, so a translation cannot be
#: stored longer than the text it translates.
_NAME_MAX_CHARS = 64


@dataclass(frozen=True)
class _Taxonomy:
    """Everything that differs between the two route groups."""

    model: type[SkillCategory] | type[ConnectorCategory]
    target_type: str
    create_action: AuditAction
    update_action: AuditAction


_SKILL = _Taxonomy(
    model=SkillCategory,
    target_type="skill_category",
    create_action=AuditAction.SKILL_CATEGORY_CREATE,
    update_action=AuditAction.SKILL_CATEGORY_UPDATE,
)
_CONNECTOR = _Taxonomy(
    model=ConnectorCategory,
    target_type="connector_category",
    create_action=AuditAction.CONNECTOR_CATEGORY_CREATE,
    update_action=AuditAction.CONNECTOR_CATEGORY_UPDATE,
)


def _name_fields(current_name: str, body: CategoryUpdate) -> dict[str, Any]:
    """Resolve what an edit writes to the ``name`` / ``name_i18n`` pair.

    Three cases, and the middle one is the invalidation rule from
    :mod:`app.services.localized_text` — a map left behind shadows the
    new text for *every* locale, so a rename that says nothing about
    translations drops them:

    ===============================  ==========================================
    renamed, ``name_i18n`` sent      write both
    renamed, ``name_i18n`` absent    write the name, clear the map
    not renamed, ``name_i18n`` sent  replace the map; the column did not move
    ===============================  ==========================================

    Unlike the catalog — which discards a locale map on every rename
    because its console can neither show nor edit one — this surface
    lets the operator supply the new translations in the same request.
    A category *is* its name, so silently dropping the map on every
    rename would make the console a half-editor and the translations
    recoverable only by SQL.

    Split out rather than inlined so the three cases sit together with
    the rule they implement, instead of interleaved with the fields that
    have no rule at all.
    """
    fields: dict[str, Any] = {}
    renamed = body.name is not None and body.name != current_name
    if renamed:
        fields["name"] = body.name

    if "name_i18n" in body.model_fields_set:
        fields["name_i18n"] = localized_text.clean_map(body.name_i18n, max_chars=_NAME_MAX_CHARS)
    elif renamed:
        fields["name_i18n"] = None
    return fields


async def _list(ctx: StaffContext, tax: _Taxonomy) -> list[CategoryResponse]:
    """Return the whole taxonomy in display order, unpublished included.

    Not paginated, for the reason ``list_catalog`` is not: a handful of
    hand-ordered rows, where paging would hide the thing being ordered.

    Tie-broken on ``id``, matching the public tab listings
    (``api/public/skill.py``, ``api/public/mcp_server.py``) rather than on
    ``name``. ``sort_order`` defaults to 0 for every row, so the tie is the
    normal case, not the edge one — and an operator reordering against a
    list that is not the one users see is reordering blind.
    """
    rows = (
        await ctx.db.scalars(select(tax.model).order_by(tax.model.sort_order, tax.model.id))
    ).all()
    return [CategoryResponse.model_validate(row) for row in rows]


async def _create(ctx: StaffContext, tax: _Taxonomy, body: CategoryCreate) -> CategoryResponse:
    """Add a category, published unless the operator says otherwise.

    A duplicate name is left to ``uq_*_category_name`` and surfaces as
    the generic 409: the operator is looking at the handful of names
    already on the screen, so this is a typo they can see, not a
    conflict with a row they cannot.
    """
    async with audit(ctx, action=tax.create_action, target_type=tax.target_type) as draft:
        row = tax.model(
            name=body.name,
            name_i18n=localized_text.clean_map(body.name_i18n, max_chars=_NAME_MAX_CHARS),
            published=body.published,
            sort_order=body.sort_order,
        )
        ctx.db.add(row)
        await ctx.db.flush()
        draft.target_id = row.id
        draft.payload = {"name": row.name, "published": row.published}

    await ctx.db.refresh(row)
    return CategoryResponse.model_validate(row)


async def _update(
    ctx: StaffContext, tax: _Taxonomy, category_id: int, body: CategoryUpdate
) -> CategoryResponse:
    """Rename, retranslate, publish/unpublish, or reorder a category."""
    # ``get`` widens ``type[A] | type[B]`` to their shared ``Base``.
    row = cast(
        "SkillCategory | ConnectorCategory | None",
        await ctx.db.get(tax.model, category_id),
    )
    if row is None:
        raise CategoryNotFoundError(f"No {tax.target_type} with id {category_id}.")

    # ``name`` and ``name_i18n`` are excluded from the generic dump and
    # resolved together instead: the pair has a rule the other fields do
    # not, and applying it to one column without the other is the bug
    # ``_name_fields`` exists to prevent.
    fields = body.model_dump(exclude_none=True, exclude={"name", "name_i18n"})
    fields |= _name_fields(row.name, body)
    # Raw values: every column here is JSON-native, and ``str()`` on
    # ``name_i18n`` would put a Python repr of the map into a JSONB payload
    # someone has to grep.
    changes = {
        field: {"from": getattr(row, field), "to": value}
        for field, value in fields.items()
        if getattr(row, field) != value
    }
    if not changes:
        return CategoryResponse.model_validate(row)

    async with audit(
        ctx,
        action=tax.update_action,
        target_type=tax.target_type,
        target_id=row.id,
    ) as draft:
        for field, value in fields.items():
            setattr(row, field, value)
        # ``category_name`` rather than ``name``: the name is editable,
        # so under one key the label and the before → after entry would
        # overwrite each other (same reason as the connector catalog's).
        draft.payload = {"category_name": row.name, **changes}

    await ctx.db.refresh(row)
    return CategoryResponse.model_validate(row)


@router.get("/skill-categories", response_model=list[CategoryResponse])
async def list_skill_categories(ctx: StaffContext) -> list[CategoryResponse]:
    """Every skills tab in display order, unpublished included."""
    return await _list(ctx, _SKILL)


@router.post(
    "/skill-categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_skill_category(ctx: StaffContext, body: CategoryCreate) -> CategoryResponse:
    """Add a skills tab, published unless the body says otherwise."""
    return await _create(ctx, _SKILL, body)


@router.patch("/skill-categories/{category_id}", response_model=CategoryResponse)
async def update_skill_category(
    ctx: StaffContext, category_id: int, body: CategoryUpdate
) -> CategoryResponse:
    """Rename, retranslate, publish/unpublish, or reorder a skills tab.

    A rename that carries no ``name_i18n`` clears the one it had — see
    ``_name_fields``.
    """
    return await _update(ctx, _SKILL, category_id, body)


@router.get("/connector-categories", response_model=list[CategoryResponse])
async def list_connector_categories(ctx: StaffContext) -> list[CategoryResponse]:
    """Every connectors tab in display order, unpublished included."""
    return await _list(ctx, _CONNECTOR)


@router.post(
    "/connector-categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connector_category(ctx: StaffContext, body: CategoryCreate) -> CategoryResponse:
    """Add a connectors tab, published unless the body says otherwise."""
    return await _create(ctx, _CONNECTOR, body)


@router.patch("/connector-categories/{category_id}", response_model=CategoryResponse)
async def update_connector_category(
    ctx: StaffContext, category_id: int, body: CategoryUpdate
) -> CategoryResponse:
    """Rename, retranslate, publish/unpublish, or reorder a connectors tab.

    A rename that carries no ``name_i18n`` clears the one it had — see
    ``_name_fields``.
    """
    return await _update(ctx, _CONNECTOR, category_id, body)
