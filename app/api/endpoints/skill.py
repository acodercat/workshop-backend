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
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import and_, func, or_, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.skill import Skill, SkillOrigin
from app.models.user import User
from app.models.user_skill_preference import UserSkillPreference
from app.rate_limit import limiter
from app.schemas.file import DownloadUrlResponse
from app.schemas.skill import SkillResponse, SkillUpdate
from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.errors import (
    InvalidSkillBundleError,
    SkillBundleTooLargeError,
    SkillNameConflictError,
    SkillQuotaExceededError,
)
from app.services.skill_bundle import (
    InvalidSkillBundleError as ServiceInvalidSkillBundleError,
    normalize_bundle,
    parse_bundle,
    parse_skill_md,
    validate_zip_structure,
)

logger = logging.getLogger(__name__)

# Per-user CUSTOM skill count cap. Bounds account-level abuse (a real
# user has a handful of skills; 20 is comfortable headroom). Paired
# with the per-bundle ``SKILL_POLICY.max_bytes`` cap so total OSS
# storage per user is implicitly bounded too.
_MAX_SKILLS_PER_USER = 20

router = APIRouter(tags=["skills"])


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
    """
    filename = file.filename or "skill.zip"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SKILL_POLICY.allowed_types:
        raise InvalidSkillBundleError(
            f"unsupported file extension {ext!r}; "
            f"allowed: {', '.join(sorted(SKILL_POLICY.allowed_types))}"
        )
    is_zip = ext == ".zip"
    bundle_bytes = await file.read()

    if len(bundle_bytes) > SKILL_POLICY.max_bytes:
        raise SkillBundleTooLargeError(
            f"file is {len(bundle_bytes)} bytes; limit is {SKILL_POLICY.max_bytes}"
        )

    # Validate structure (zip-bomb, path traversal, symlinks) **before**
    # any path that decompresses entry bytes. ``normalize_bundle``
    # repacks the archive and ``parse_bundle`` reads SKILL.md — both
    # would let a malicious wrapper inflate memory if we let them run
    # on an unvalidated input.
    try:
        if is_zip:
            validate_zip_structure(bundle_bytes)
            normalized = normalize_bundle(bundle_bytes)
            if normalized is not None:
                bundle_bytes = normalized
            parsed = parse_bundle(bundle_bytes)
        else:
            parsed = parse_skill_md(bundle_bytes)
    except ServiceInvalidSkillBundleError as exc:
        raise InvalidSkillBundleError(str(exc)) from exc

    await _enforce_user_quota(db, current_user.id)

    # Upload to OSS under a user-scoped key, then INSERT the row in a
    # try/except that compensates the upload if the INSERT fails (DB
    # constraint, race, transient error). Without this an
    # ``IntegrityError`` would leave an orphan bundle paying OSS storage
    # forever.
    #
    # The filename segment of the key is **derived** from the parsed
    # skill name + canonical (lowercased) extension — NOT the user's
    # original filename. The user-supplied filename was only inspected
    # to decide the ``is_zip`` branch; reusing it verbatim in the key
    # would leak case (``MySkill.ZIP``) and odd characters through to
    # every consumer that dispatches on ``bundle_key.endsWith(".zip")``.
    bundle_filename = f"{parsed.name}{ext}"
    bundle_key = f"{SKILL_POLICY.key_prefix}/{current_user.id}/{uuid.uuid4()}/{bundle_filename}"
    await asyncio.to_thread(
        file_storage.upload, SKILL_POLICY, bundle_key, bundle_bytes,
    )
    skill = Skill(
        user_id=current_user.id,
        origin=SkillOrigin.CUSTOM,
        name=parsed.name,
        display_name=parsed.name,
        description=parsed.description,
        bundle_key=bundle_key,
        bundle_size=len(bundle_bytes),
    )
    db.add(skill)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await _best_effort_delete_oss(bundle_key)
        if isinstance(exc, IntegrityError):
            raise SkillNameConflictError(
                f"a skill named {parsed.name!r} already exists for this user"
            ) from exc
        raise

    await db.refresh(skill)
    logger.info(
        "skill registered: id=%s user_id=%s name=%r size=%d",
        skill.id, current_user.id, skill.name, skill.bundle_size,
    )
    return SkillResponse.model_validate(skill)


@router.get("/skills")
async def list_skills(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[SkillResponse]:
    """Return skills visible to the authenticated user: their own
    CUSTOM uploads + all BUILTIN skills. Newest first.

    The ``enabled`` field in the response is the **effective** state
    for this user: a ``user_skill_preference`` row overrides
    ``skill.enabled`` when present. That row only ever exists for
    BUILTINs the user has toggled (CUSTOM uses ``skill.enabled``
    directly, owner-mutated).
    """
    stmt = (
        select(Skill, UserSkillPreference.enabled.label("preference_enabled"))
        .outerjoin(
            UserSkillPreference,
            and_(
                UserSkillPreference.skill_id == Skill.id,
                UserSkillPreference.user_id == current_user.id,
            ),
        )
        .where(
            (Skill.user_id == current_user.id)
            | (Skill.origin == SkillOrigin.BUILTIN)
        )
        .order_by(Skill.created_at.desc())
    )
    result = await db.execute(stmt)
    responses: list[SkillResponse] = []
    for skill, preference_enabled in result.all():
        response = SkillResponse.model_validate(skill)
        if preference_enabled is not None:
            response.enabled = preference_enabled
        responses.append(response)
    return responses


@router.patch("/skills/{skill_id}")
async def update_skill(
    skill_id: int,
    body: SkillUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SkillResponse:
    """Edit a skill's picker-visible metadata or flip its enable toggle.

    Three cases:

    - **CUSTOM owned by caller** — full edit (display_name, description,
      enabled all writable on the ``skill`` row).
    - **BUILTIN** — only ``enabled`` is writable, via a per-user
      ``user_skill_preference`` row (the platform row stays shared).
      ``display_name`` / ``description`` reject with 403 because they'd
      need to mutate the shared row.
    - **CUSTOM not owned** — 404 via :func:`_get_visible_skill`.

    The bundle itself is immutable on every path — re-upload for content
    changes.
    """
    skill = await _get_visible_skill(db, skill_id, current_user.id)

    if skill.origin == SkillOrigin.BUILTIN:
        if body.display_name is not None or body.description is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot edit metadata of a BUILTIN skill",
            )
        if body.enabled is not None:
            await _upsert_preference(
                db, current_user.id, skill.id, body.enabled,
            )
        await db.commit()
        response = SkillResponse.model_validate(skill)
        if body.enabled is not None:
            response.enabled = body.enabled
        return response

    # CUSTOM owned by the caller. Collect non-null fields and apply
    # them in a single UPDATE statement — avoids the read-modify-write
    # race where two concurrent PATCHes both load the ORM object, each
    # mutates one field, and the second commit wipes the first.
    updates = {
        field: value
        for field, value in body.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if updates:
        await db.execute(
            sql_update(Skill).where(Skill.id == skill.id).values(**updates)
        )
        await db.commit()
        await db.refresh(skill)
    return skill


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

    Accessible for BUILTIN skills (visible to everyone) AND the user's
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
    """Fetch a skill visible to ``user_id`` (own CUSTOM or any BUILTIN).

    Used by PATCH (BUILTIN toggle path uses ``user_skill_preference``;
    the row itself is shared) and GET /skills/{id}/download-url — both
    operate on the wider "visible" set than the owner-only ``_get_owned_skill``.
    """
    stmt = select(Skill).where(
        Skill.id == skill_id,
        or_(Skill.user_id == user_id, Skill.origin == SkillOrigin.BUILTIN),
    )
    result = await db.execute(stmt)
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="skill not found",
        )
    return skill


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


async def _enforce_user_quota(db: AsyncSession, user_id: int) -> None:
    """Reject the upload if the user is at the per-user skill count cap.

    Counts the user's CUSTOM rows only — BUILTIN rows have
    ``user_id IS NULL`` so they don't count. Race-acceptable: two
    concurrent uploads at the boundary could both succeed, leaving
    the user one over. Not worth a row lock for a soft policy limit.
    """
    used = await db.scalar(
        select(func.count(Skill.id)).where(Skill.user_id == user_id)
    ) or 0
    if used >= _MAX_SKILLS_PER_USER:
        raise SkillQuotaExceededError(
            f"user already has {used} skills; "
            f"per-user limit is {_MAX_SKILLS_PER_USER}"
        )


async def _best_effort_delete_oss(bundle_key: str) -> None:
    """Compensating delete after a failed register_skill DB write.

    Failures here are logged and swallowed — the DB rolled back, so
    the user-visible state is "upload didn't happen"; an orphan OSS
    object is only a storage-cost issue and should be picked up by a
    bucket lifecycle rule.
    """
    try:
        await asyncio.to_thread(file_storage.delete, SKILL_POLICY, bundle_key)
    except Exception as exc:
        logger.warning(
            "skill upload rollback: OSS cleanup failed for %s: %s",
            bundle_key, exc,
        )
