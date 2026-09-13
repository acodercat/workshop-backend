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
import os

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
from app.models.user import User
from app.models.user_skill_preference import UserSkillPreference
from app.rate_limit import limiter
from app.schemas.file import DownloadUrlResponse
from app.schemas.skill import SkillResponse, SkillUpdate
from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_enablement import (
    ensure_enable_capacity,
    preference_join,
)
from app.services.skill_i18n import (
    DEFAULT_LOCALE,
    normalize_locale,
    resolve_display_name,
)
from app.services.skill_registration import register_skill_bundle

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


def _to_response(
    skill: Skill, *, enabled: bool, display_name: str | None = None,
) -> SkillResponse:
    """One skill as its owner's picker sees it.

    ``enabled`` is required rather than read off the row: it lives in
    ``user_skill_preference``, so it is the caller's to supply and a
    route that forgot would otherwise report a plausible lie.

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
    # A fresh upload has no preference row: usable by an explicit pick
    # and silent otherwise, until its owner turns it on.
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

    ``enabled`` is this user's own answer — their
    ``user_skill_preference`` row, or ``false`` where they have never
    said. It is what the toggle on the skills page shows, and (combined
    with the listing being live, which every row here already is) what
    decides whether the skill rides in their context every turn.
    """
    resolved_locale = normalize_locale(locale)
    stmt = (
        select(Skill, UserSkillPreference.enabled.label("preference_enabled"))
        .outerjoin(UserSkillPreference, preference_join(current_user.id))
        .where(
            Skill.status == SkillStatus.PUBLISHED,
            (Skill.user_id == current_user.id)
            | (Skill.origin == SkillOrigin.OFFICIAL),
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
      one; neither is a reason for the toggle to live anywhere else.
    - ``display_name`` / ``description`` are the listing's own metadata.
      Only its owner may edit them, so on an ``OFFICIAL`` skill they are
      403 — the platform owns that row.

    A skill the caller cannot see, or one that is not live, is 404 via
    :func:`_get_visible_skill`. Enabling is capped at
    ``skill_enablement.MAX_ENABLED_SKILLS`` (403
    ``skill_enable_quota_exceeded``). The bundle itself is immutable
    here — re-upload for content changes.
    """
    skill = await _get_visible_skill(db, skill_id, current_user.id)

    edits_metadata = body.display_name is not None or body.description is not None
    if edits_metadata and skill.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot edit metadata of an OFFICIAL skill",
        )
    # Both checks run before anything is written — authorization, then
    # the budget — so a request that cannot have what it asked for does
    # not do half its work first. Excluding this skill from the count
    # keeps re-enabling an already-on skill a no-op rather than a
    # spurious rejection at the boundary.
    if body.enabled:
        await ensure_enable_capacity(
            db, current_user.id, exclude_skill_id=skill.id,
        )

    if edits_metadata:
        # One UPDATE rather than ORM mutation: avoids the
        # read-modify-write race where two concurrent PATCHes each load
        # the row, mutate one field, and the second commit wipes the
        # first. An explicit rename must also win over a bundle-shipped
        # locale map — the list endpoint resolves ``display_name_i18n``
        # first, so a stale map would silently shadow the edit.
        updates: dict[str, object] = {}
        if body.display_name is not None:
            updates["display_name"] = body.display_name
            updates["display_name_i18n"] = None
        if body.description is not None:
            updates["description"] = body.description
        await db.execute(
            sql_update(Skill).where(Skill.id == skill.id).values(**updates)
        )

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
        skill_id, current_user.id, skill_name,
    )

    try:
        await asyncio.to_thread(file_storage.delete, SKILL_POLICY, bundle_key)
    except Exception as exc:
        # Don't fail the request — the DB row is gone, which is the
        # user-visible state. Leftover OSS object is cleanup-cron's job.
        logger.warning(
            "skill delete: OSS cleanup failed for %s (skill id=%s): %s",
            bundle_key, skill_id, exc,
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
    # ``skill.name`` is the kebab-case identifier — file-system friendly
    # and stable. The original upload's filename (and the UUID-suffixed
    # OSS key) are meaningless to the user; reuse only its extension.
    extension = os.path.splitext(skill.bundle_key)[1] or ".zip"
    filename = f"{skill.name}{extension}"
    url, expires_in = file_storage.generate_download_url(
        SKILL_POLICY, skill.bundle_key, filename,
    )
    return DownloadUrlResponse(
        download_url=url, file_key=skill.bundle_key, expires_in=expires_in,
    )


async def _get_owned_skill(
    db: AsyncSession, skill_id: int, user_id: int,
) -> Skill:
    """Fetch a CUSTOM skill that belongs to ``user_id``.

    Used by DELETE — only the owner can drop their CUSTOM bundle.
    """
    stmt = select(Skill).where(
        Skill.id == skill_id, Skill.user_id == user_id,
    )
    result = await db.execute(stmt)
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="skill not found",
        )
    return skill


async def _get_visible_skill(
    db: AsyncSession, skill_id: int, user_id: int,
) -> Skill:
    """Fetch a live skill visible to ``user_id`` (own CUSTOM or OFFICIAL).

    Used by PATCH and GET /skills/{id}/download-url — both operate on
    the wider "visible" set than the owner-only ``_get_owned_skill``.
    SYSTEM skills are agent-only, so they resolve to 404 here just like
    a stranger's CUSTOM — and so does anything an operator has retired
    or not yet published: a listing that is not live is not addressable.
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
            status_code=status.HTTP_404_NOT_FOUND, detail="skill not found",
        )
    return skill


async def _preference_enabled(
    db: AsyncSession, user_id: int, skill_id: int,
) -> bool:
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
    db: AsyncSession, user_id: int, skill_id: int, enabled: bool,
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
                UserSkillPreference.user_id, UserSkillPreference.skill_id,
            ],
            set_={"enabled": enabled, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
