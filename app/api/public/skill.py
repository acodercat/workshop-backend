"""User-uploaded skill bundle CRUD.

Upload is a single multipart POST: the client sends the .zip or .md
file directly, the backend validates, normalizes wrapper zips,
uploads to OSS, and creates the DB row — one request, one response.

The bundle itself is **immutable** through this surface — edits to a
skill require a new upload (gives the worker a clean cache key and
avoids ambiguity about which version a running session staged). The
PATCH endpoint only changes picker-visible metadata (name /
description) and the enable toggle.
"""

import asyncio
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, or_, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.api.uploads import read_capped
from app.auth import authenticate_and_get_user
from app.models.skill import Skill, SkillOrigin, SkillStatus
from app.models.skill_category import SkillCategory
from app.models.user import User
from app.models.user_skill_preference import UserSkillPreference
from app.rate_limit import limiter
from app.schemas.file import DownloadUrlResponse
from app.schemas.skill import SkillCategoryResponse, SkillResponse, SkillUpdate
from app.services import file_storage, localized_text
from app.services.file_storage import SKILL_POLICY
from app.services.skill_download import skill_download_url
from app.services.skill_enablement import preference_join
from app.services.skill_i18n import (
    DEFAULT_LOCALE,
    normalize_locale,
    resolve_display_name,
)
from app.services.skill_registration import register_skill_bundle

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


def _to_response(skill: Skill, *, enabled: bool, display_name: str | None = None) -> SkillResponse:
    """One skill as a user sees it — list, upload and PATCH replies alike.

    ``enabled`` is required rather than read off the row: it lives in
    ``user_skill_preference``, so it is the caller's to supply and a
    route that forgot would otherwise report a plausible lie. Mirrors
    ``api.admin.skill._to_skill_response``, which builds the console's
    view of the same row for the same reason.

    ``display_name`` overrides the stored default where the caller has
    already resolved it for a locale.
    """
    return SkillResponse(
        id=skill.id,
        origin=skill.origin,
        name=skill.name,
        display_name=display_name or skill.display_name,
        description=skill.description,
        bundle_size=skill.bundle_size,
        enabled=enabled,
        category_id=skill.category_id,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.post("/skills", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def register_skill(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SkillResponse:
    """Upload + register a skill bundle in one request.

    Accepts a ``.zip`` or ``.md`` file via multipart. The backend
    validates, normalizes (flattens wrapper zips), uploads to OSS,
    and creates the DB row.

    400 if the file is malformed, lacks SKILL.md, or has unsafe entries.
    400 if the file extension isn't in the SKILL_POLICY whitelist.
    403 if the user is already at the per-user skill count cap.
    409 if the extracted skill name collides with one this user
        already has.
    413 if the upload exceeds ``SKILL_POLICY.max_bytes``.
    """
    data = await read_capped(file, SKILL_POLICY.max_bytes)
    skill = await register_skill_bundle(
        db,
        user_id=current_user.id,
        filename=file.filename or "skill.zip",
        data=data,
    )
    # A fresh upload has no preference row, so it is not in its owner's
    # composer picker until they turn it on. Reachable meanwhile by a
    # request that names it directly — a pick is its own authorization.
    return _to_response(skill, enabled=False)


@router.get("/skills")
async def list_skills(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    locale: str = Query(default=DEFAULT_LOCALE),
) -> list[SkillResponse]:
    """Return skills visible to the authenticated user: their own
    CUSTOM uploads + all OFFICIAL skills. Newest first.

    SYSTEM skills are agent-only and never returned here.

    ``display_name`` is resolved for the ``locale`` query param (the frontend
    proxy forwards the app UI language): an OFFICIAL skill with a
    ``display_name_i18n`` map returns its localized name, everything else the
    stored default. The i18n map itself is never exposed.

    Only live listings appear: a skill an operator has retired or not
    yet published is gone from here for everyone, including the users
    who had turned it on.

    ``enabled`` is this user's own answer — their ``user_skill_preference``
    row, or ``false`` where they have never said. It is what the toggle on
    the skills page shows, and what decides whether the skill is offered
    in the composer's picker. It does **not** load the skill: what reaches
    the model is the agent-only ``SYSTEM`` set plus whatever the user
    picked for that turn.

    Every row is returned whatever that answer is — the skills page is
    where the answer gets given, so filtering by it here would hide the
    switch from the only screen that has one. The composer filters the
    same payload down to the enabled ones.
    """
    resolved_locale = normalize_locale(locale)
    stmt = (
        select(Skill, UserSkillPreference.enabled.label("preference_enabled"))
        .outerjoin(UserSkillPreference, preference_join(current_user.id))
        .where(
            Skill.status == SkillStatus.PUBLISHED,
            (Skill.user_id == current_user.id) | (Skill.origin == SkillOrigin.OFFICIAL),
        )
        .order_by(Skill.created_at.desc())
    )
    result = await db.execute(stmt)
    return [
        _to_response(
            skill,
            enabled=bool(preference_enabled),
            display_name=resolve_display_name(
                skill.display_name, skill.display_name_i18n, resolved_locale
            ),
        )
        for skill, preference_enabled in result.all()
    ]


@router.get("/skills/categories")
async def list_skill_categories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
    locale: str = Query(default=DEFAULT_LOCALE),
) -> list[SkillCategoryResponse]:
    """The tabs the skills page groups OFFICIAL skills under.

    Declared before the parameterized ``/skills/{skill_id}`` routes, as
    literal segments are throughout this codebase.

    **The array order is the tab order** — rendered as given, not sorted
    by name, which would undo the ordering the operator set.

    Two things the client owns rather than this endpoint. A
    ``category_id`` it cannot resolve here reads as uncategorised: an
    unpublished category keeps its rows, so the tab returns intact when
    it is republished. And an empty category renders an empty tab the
    client drops on its own — it holds both lists already, where
    filtering here would mean a subquery on every request.
    """
    resolved_locale = normalize_locale(locale)
    rows = (
        await db.scalars(
            select(SkillCategory)
            .where(SkillCategory.published.is_(True))
            .order_by(SkillCategory.sort_order, SkillCategory.id)
        )
    ).all()
    return [_localized_category_response(row, resolved_locale) for row in rows]


def _localized_category_response(category: SkillCategory, locale: str) -> SkillCategoryResponse:
    """One tab as the client sees it, in *locale*.

    A module-level named function, not an inline comprehension: it is
    what lets the resolution be tested without a database.
    """
    response = SkillCategoryResponse.model_validate(category)
    response.name = localized_text.resolve(category.name, category.name_i18n, locale)
    return response


@router.patch("/skills/{skill_id}")
async def update_skill(
    skill_id: int,
    body: SkillUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SkillResponse:
    """Edit a skill's picker-visible metadata, or flip this user's toggle.

    The two halves of the body go to two different rows, which is why
    they answer to different rules:

    - ``enabled`` is this caller's own answer and always lands in their
      ``user_skill_preference`` row, whatever the skill's origin. An
      ``OFFICIAL`` row is shared, and a ``CUSTOM`` one has an audience of
      one; neither is a reason for the toggle to live anywhere else. It
      decides whether the composer offers the skill, not whether the model
      loads it, so there is no quota on it — nothing per-turn is spent.
    - ``display_name`` / ``description`` are the listing's own metadata.
      Only its owner may edit them, so on an ``OFFICIAL`` skill they are
      403 — the console owns that row.

    A skill the caller cannot see, or one that is not live, is 404 via
    :func:`_get_visible_skill`. The bundle itself is immutable here —
    re-upload for content changes.
    """
    skill = await _get_visible_skill(db, skill_id, current_user.id)

    edits_metadata = body.display_name is not None or body.description is not None
    if edits_metadata and skill.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot edit metadata of an OFFICIAL skill",
        )
    if edits_metadata:
        # One UPDATE rather than ORM mutation: avoids the
        # read-modify-write race where two concurrent PATCHes each load
        # the row, mutate one field, and the second commit wipes the
        # first. Clearing the locale map is not optional — see the
        # invalidation rule in ``app.services.localized_text``.
        updates: dict[str, object] = {}
        if body.display_name is not None:
            updates["display_name"] = body.display_name
            updates["display_name_i18n"] = None
        if body.description is not None:
            updates["description"] = body.description
        await db.execute(sql_update(Skill).where(Skill.id == skill.id).values(**updates))

    if body.enabled is not None:
        await _upsert_preference(db, current_user.id, skill.id, body.enabled)

    await db.commit()
    await db.refresh(skill)
    return _to_response(
        skill,
        enabled=await _preference_enabled(db, current_user.id, skill.id),
    )


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Delete a skill row and best-effort drop its OSS bundle.

    The OSS delete is intentionally outside the DB transaction: a
    stale OSS object (DB row gone, bundle remains) is recoverable
    by a future cleanup cron; a missing OSS object with a live DB
    row would corrupt the worker's staging step. Order: DB first,
    then OSS.
    """
    skill = await _get_owned_skill(db, skill_id, current_user.id)
    bundle_key = skill.bundle_key
    skill_name = skill.name
    await db.delete(skill)
    await db.commit()
    logger.info(
        "skill deleted: id=%s user_id=%s name=%r",
        skill_id,
        current_user.id,
        skill_name,
    )

    try:
        await asyncio.to_thread(file_storage.delete, SKILL_POLICY, bundle_key)
    except Exception as exc:
        # Don't fail the request — the DB row is gone, which is the
        # user-visible state. Leftover OSS object is cleanup-cron's job.
        logger.warning(
            "skill delete: OSS cleanup failed for %s (skill id=%s): %s",
            bundle_key,
            skill_id,
            exc,
        )


@router.get("/skills/{skill_id}/download-url", response_model=DownloadUrlResponse)
async def get_skill_download_url(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> DownloadUrlResponse:
    """Presigned GET URL for downloading the skill bundle from OSS.

    The frontend fetches the bundle directly from OSS and parses it
    client-side (JSZip for .zip, plain text for .md) to render the
    file tree + content preview.

    Accessible for OFFICIAL skills (visible to everyone) AND the user's
    own CUSTOM skills — mirrors the visibility of ``GET /skills``.
    """
    skill = await _get_visible_skill(db, skill_id, current_user.id)
    return skill_download_url(skill)


async def _get_owned_skill(
    db: AsyncSession,
    skill_id: int,
    user_id: int,
) -> Skill:
    """Fetch a CUSTOM skill that belongs to ``user_id``.

    Used by DELETE — only the owner can drop their CUSTOM bundle.
    """
    stmt = select(Skill).where(
        Skill.id == skill_id,
        Skill.user_id == user_id,
    )
    result = await db.execute(stmt)
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="skill not found",
        )
    return skill


async def _get_visible_skill(
    db: AsyncSession,
    skill_id: int,
    user_id: int,
) -> Skill:
    """Fetch a live skill visible to ``user_id`` (own CUSTOM or any OFFICIAL).

    Used by PATCH and GET /skills/{id}/download-url — both operate on the
    wider "visible" set than the owner-only ``_get_owned_skill``. SYSTEM
    skills are agent-only, so they resolve to 404 here just like a
    stranger's CUSTOM — and so does anything an operator has retired or
    not yet published: a listing that is not live is not addressable.
    """
    stmt = select(Skill).where(
        Skill.id == skill_id,
        Skill.status == SkillStatus.PUBLISHED,
        or_(Skill.user_id == user_id, Skill.origin == SkillOrigin.OFFICIAL),
    )
    result = await db.execute(stmt)
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="skill not found",
        )
    return skill


async def _preference_enabled(db: AsyncSession, user_id: int, skill_id: int) -> bool:
    """This user's answer for one skill; ``False`` where they never gave one."""
    return bool(
        await db.scalar(
            select(UserSkillPreference.enabled).where(
                UserSkillPreference.user_id == user_id,
                UserSkillPreference.skill_id == skill_id,
            )
        )
    )


async def _upsert_preference(
    db: AsyncSession,
    user_id: int,
    skill_id: int,
    enabled: bool,
) -> None:
    """Idempotent ``INSERT ... ON CONFLICT`` for the override row.

    ``updated_at`` is bumped explicitly in the conflict branch — the
    ``onupdate=func.now()`` mapping on the model only fires on ORM
    UPDATE statements, not on Postgres ``ON CONFLICT DO UPDATE``.
    """
    stmt = (
        pg_insert(UserSkillPreference)
        .values(user_id=user_id, skill_id=skill_id, enabled=enabled)
        .on_conflict_do_update(
            index_elements=[
                UserSkillPreference.user_id,
                UserSkillPreference.skill_id,
            ],
            set_={"enabled": enabled, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
