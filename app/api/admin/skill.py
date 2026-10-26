"""The console's skill surface.

- ``GET   /skills?origin=&q=`` — every origin, including the ``SYSTEM``
  skills hidden from users entirely.
- ``POST  /skills``            — upload a platform bundle (new or a new
  version of one that exists).
- ``PATCH /skills/{id}``       — flip a skill's platform-wide default.
- ``GET   /skills/{id}/download-url`` — presigned GET for a **platform**
  bundle.

Download refuses ``CUSTOM``. A user's upload is their private file —
the same class of thing as a conversation, which this console does not
read — while a ``SYSTEM`` or ``OFFICIAL`` bundle is the platform's own
artifact, and being able to pull it back is what makes a displaced
bundle a rollback rather than a note. Refusing ``CUSTOM`` is what keeps
that rule intact: nothing this surface reads is a customer's.

Upload goes through
:func:`~app.services.skill_registration.register_platform_skill_bundle`,
the same funnel ``scripts/upload_skill.py`` uses — validation, the OSS
write and the compensating delete are not re-implemented here, so a
browser upload and a script upload produce the same row and the same
object. The script stays as the recovery path for a console that is
down or an operator who has only SSH.
"""

import logging
from typing import cast, get_args

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select

from app.api.admin.dependencies import StaffContext
from app.api.uploads import form_field, multipart, read_capped, upload_request_body, uploaded_file
from app.common.pagination import paginate
from app.errors import (
    CategoryNotFoundError,
    PrivateSkillBundleError,
    PrivateSkillLifecycleError,
    SkillNotFoundError,
)
from app.models.skill import Skill, SkillOrigin
from app.models.skill_category import SkillCategory
from app.models.user import User
from app.schemas.admin.skill import PlatformSkillOrigin, SkillResponse, SkillUpdate
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.file import DownloadUrlResponse
from app.services import user as user_service
from app.services.file_storage import SKILL_POLICY
from app.services.skill_download import skill_download_url
from app.services.skill_registration import register_platform_skill_bundle

router = APIRouter(tags=["skills"])
logger = logging.getLogger(__name__)

#: Origins whose bundle the console may pull the bytes of. An allowlist,
#: not ``!= CUSTOM``: this is a privacy boundary, and an origin added
#: later must be refused until someone decides it belongs here rather
#: than become downloadable by default. Deliberately not derived from
#: ``PlatformSkillOrigin``, which answers a different question — who may
#: *upload* — and would tie two rules that only happen to agree today.
_DOWNLOADABLE_ORIGINS = frozenset({SkillOrigin.SYSTEM, SkillOrigin.OFFICIAL})

#: Origins whose lifecycle this surface owns. An allowlist for the same
#: reason as the one above, and a separate one for the same reason too:
#: "may we read these bytes" and "is it ours to publish" are different
#: questions that only happen to agree today.
_LIFECYCLE_ORIGINS = frozenset({SkillOrigin.SYSTEM, SkillOrigin.OFFICIAL})


def _to_skill_response(
    skill: Skill, owner_username: str | None, owner_name: str | None
) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        origin=skill.origin,
        name=skill.name,
        display_name=skill.display_name,
        description=skill.description,
        status=skill.status,
        bundle_size=skill.bundle_size,
        category_id=skill.category_id,
        user_id=skill.user_id,
        owner_username=owner_username,
        owner_name=owner_name,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.get("/skills", response_model=PaginatedResponse[SkillResponse])
async def list_skills(
    ctx: StaffContext,
    origin: SkillOrigin | None = Query(None),
    q: str | None = Query(None, min_length=1, max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse[SkillResponse]:
    """Return one page of skills, platform origins first then newest.

    Platform-first ordering because that is the working set: the
    catalogue an operator curates, with user uploads below it. The
    owner is LEFT-joined — platform rows have none by construction.
    """
    filters = []
    if origin is not None:
        filters.append(Skill.origin == origin)
    if q is not None:
        pattern = f"%{q}%"
        filters.append(sa.or_(Skill.name.ilike(pattern), Skill.display_name.ilike(pattern)))

    total = await ctx.db.scalar(select(func.count()).select_from(Skill).where(*filters))
    rows = (
        await ctx.db.execute(
            paginate(
                select(Skill, User.username, User.name)
                .outerjoin(User, User.id == Skill.user_id)
                .where(*filters)
                .order_by(
                    # Platform rows (no owner) first, then newest. ``id``
                    # breaks the ties: platform skills are published by
                    # one script and share a timestamp by the hundred,
                    # and OFFSET paging over a tie is free to show a row
                    # on two pages or none.
                    Skill.user_id.is_(None).desc(),
                    Skill.created_at.desc(),
                    Skill.id.desc(),
                ),
                page,
                page_size,
            )
        )
    ).all()

    return PaginatedResponse(
        items=[_to_skill_response(skill, username, name) for skill, username, name in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post(
    "/skills",
    response_model=SkillResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": upload_request_body(
            origin={"type": "string", "enum": list(get_args(PlatformSkillOrigin))}
        )
    },
)
async def upload_skill(ctx: StaffContext, request: Request) -> SkillResponse:
    """Upload a platform skill bundle: the file in ``file``, read only once the
    operator is known, and its ``origin`` beside it.

    ``origin`` is the one thing the bundle cannot tell us and it decides
    who sees the skill: ``SYSTEM`` is agent-only core, ``OFFICIAL`` is
    offered to users as an opt-in. It is the only field this form asks
    for — a new skill always lands ``STAGED``, and going live is the
    separate, deliberate act of a PATCH.

    **Uploading a name that already exists replaces that skill's bundle**
    rather than failing: for a platform catalogue, re-uploading ``pdf``
    is shipping a new version of ``pdf``. The row is updated in place and
    its status stays where it was, because shipping a fix is a content
    change. ``origin`` is the exception, being stated on every upload: a
    replace under a different origin moves the skill.
    The response is 201 either way: a new bundle now exists either way.

    400 if the bundle is malformed or the extension is not allowed,
    413 if it exceeds ``SKILL_POLICY.max_bytes``.
    """
    async with multipart(request) as form:
        origin = _platform_origin(form_field(form, "origin"))
        file = uploaded_file(form)
        data = await read_capped(file, SKILL_POLICY.max_bytes)
        filename = file.filename or "skill.zip"

    skill, replaced_key = await register_platform_skill_bundle(
        ctx.db, origin=SkillOrigin(origin), filename=filename, data=data
    )
    await ctx.db.commit()
    if replaced_key is not None:
        # The version this one displaced: the row no longer points at it,
        # but the object is still in the bucket. This deployment keeps no
        # operator trail, so the log line is the only way back to it.
        logger.info("skill %s: bundle %s replaced %s", skill.name, skill.bundle_key, replaced_key)

    # A platform upload has no owner, so neither name column applies.
    await ctx.db.refresh(skill)
    return _to_skill_response(skill, None, None)


def _platform_origin(value: str) -> PlatformSkillOrigin:
    """The ``origin`` field, refused like a declared one would be."""
    if value not in get_args(PlatformSkillOrigin):
        raise RequestValidationError(
            [
                {
                    "type": "literal_error",
                    "loc": ("body", "origin"),
                    "msg": f"Input should be one of {', '.join(get_args(PlatformSkillOrigin))}",
                }
            ]
        )
    return cast(PlatformSkillOrigin, value)


@router.patch("/skills/{skill_id}", response_model=SkillResponse)
async def update_skill(ctx: StaffContext, skill_id: int, body: SkillUpdate) -> SkillResponse:
    """Edit a skill's picker metadata, or move it through its lifecycle.

    ``status`` applies to everyone. Retiring a skill closes every route
    to it — the skills page, ambient discovery, and an explicit composer
    pick alike — including for the users who had turned it on. That is
    the difference from the column this replaced, which only ever moved
    the default for users who had never said.

    ``display_name`` / ``description`` are the same picker metadata the
    public surface lets a user edit on their own skill and refuses on a
    platform one; this is the surface that owns the shared row. The
    bundle is untouched either way — content changes are a re-upload.
    """
    skill = await ctx.db.get(Skill, skill_id)
    if skill is None:
        raise SkillNotFoundError(f"No skill with id {skill_id}.")
    if body.status is not None and skill.origin not in _LIFECYCLE_ORIGINS:
        raise PrivateSkillLifecycleError(
            f"Skill {skill_id} belongs to a user; its status is not the console's to set."
        )

    owner_username, owner_name = await user_service.owner_identity(ctx.db, skill.user_id)

    fields = body.model_dump(exclude_none=True)
    # Put back the one field whose explicit ``null`` is an instruction
    # rather than a default — see ``SkillUpdate.category_id``.
    if "category_id" in body.model_fields_set:
        fields["category_id"] = body.category_id

    # Not left to the foreign key: an ``IntegrityError`` would reach the
    # operator as the generic 409 rather than naming the missing category.
    category_id = fields.get("category_id")
    if category_id is not None and await ctx.db.get(SkillCategory, category_id) is None:
        raise CategoryNotFoundError(f"No skill category with id {category_id}.")

    changed = {field for field, value in fields.items() if getattr(skill, field) != value}
    if not changed:
        return _to_skill_response(skill, owner_username, owner_name)

    for field in changed:
        setattr(skill, field, fields[field])
    # Clearing the map is not optional — see the invalidation rule
    # in ``app.services.localized_text``. The bundle re-supplies it
    # on the next upload, so nothing is lost for good here.
    if "display_name" in changed:
        skill.display_name_i18n = None
    await ctx.db.commit()

    await ctx.db.refresh(skill)
    return _to_skill_response(skill, owner_username, owner_name)


@router.get("/skills/{skill_id}/download-url", response_model=DownloadUrlResponse)
async def read_skill_download_url(ctx: StaffContext, skill_id: int) -> DownloadUrlResponse:
    """Presigned GET URL for a platform bundle, for save-as in a browser.

    Presigned rather than streamed through this process: the bundle is
    already in OSS, and proxying megabytes through the API to re-serve
    them would buy nothing but a slower download and a busier worker.
    The URL expires — see ``file_storage._DOWNLOAD_URL_TTL_SECONDS``.

    ``CUSTOM`` is refused with a 403, not hidden behind a 404: the row is
    on the operator's screen and pretending it does not exist would read
    as a bug. See this module's docstring for why the bytes are refused.

    Signed through :func:`~app.services.skill_download.skill_download_url`
    rather than here: the customer surface signs the same bundle, and a
    bundle saved from either has to land under one name and one policy.
    """
    skill = await ctx.db.get(Skill, skill_id)
    if skill is None:
        raise SkillNotFoundError(f"No skill with id {skill_id}.")
    if skill.origin not in _DOWNLOADABLE_ORIGINS:
        raise PrivateSkillBundleError(
            f"Skill {skill_id} is not a platform bundle and cannot be downloaded here."
        )

    return skill_download_url(skill)
