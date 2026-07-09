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
from sqlalchemy import and_, func, or_, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.errors import SkillBundleTooLargeError
from app.models.skill import Skill, SkillOrigin
from app.models.user import User
from app.models.user_skill_preference import UserSkillPreference
from app.rate_limit import limiter
from app.schemas.file import DownloadUrlResponse
from app.schemas.skill import SkillResponse, SkillUpdate
from app.services import file_storage
from app.services.file_storage import SKILL_POLICY
from app.services.skill_i18n import (
    DEFAULT_LOCALE,
    normalize_locale,
    resolve_display_name,
)
from app.services.skill_registration import register_skill_bundle

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])

# Chunk size for the bounded read below.
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read an ``UploadFile`` into memory, aborting past ``max_bytes``.

    ``UploadFile.read()`` with no argument loads the entire (possibly
    disk-spooled) body into one ``bytes`` — for a hostile multi-GB upload
    that is the OOM. Reading in bounded chunks and bailing the instant the
    running total exceeds the cap keeps peak memory at ``max_bytes`` + one
    chunk. ``register_skill_bundle`` re-checks the same cap on the returned
    bytes, so this is purely the early-abort guard.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SkillBundleTooLargeError(
                f"upload exceeds the {max_bytes}-byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
    data = await _read_capped(file, SKILL_POLICY.max_bytes)
    skill = await register_skill_bundle(
        db,
        user_id=current_user.id,
        filename=file.filename or "skill.zip",
        data=data,
    )
    return SkillResponse.model_validate(skill)


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

    The ``enabled`` field in the response is the **effective** state
    for this user: a ``user_skill_preference`` row overrides
    ``skill.enabled`` when present. That row only ever exists for
    OFFICIAL skills the user has toggled (CUSTOM uses ``skill.enabled``
    directly, owner-mutated).
    """
    resolved_locale = normalize_locale(locale)
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
            | (Skill.origin == SkillOrigin.OFFICIAL)
        )
        .order_by(Skill.created_at.desc())
    )
    result = await db.execute(stmt)
    responses: list[SkillResponse] = []
    for skill, preference_enabled in result.all():
        response = SkillResponse.model_validate(skill)
        response.display_name = resolve_display_name(
            skill.display_name, skill.display_name_i18n, resolved_locale
        )
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
    - **OFFICIAL** — only ``enabled`` is writable, via a per-user
      ``user_skill_preference`` row (the platform row stays shared).
      ``display_name`` / ``description`` reject with 403 because they'd
      need to mutate the shared row.
    - **CUSTOM not owned / SYSTEM** — 404 via :func:`_get_visible_skill`.

    The bundle itself is immutable on every path — re-upload for content
    changes.
    """
    skill = await _get_visible_skill(db, skill_id, current_user.id)

    if skill.origin == SkillOrigin.OFFICIAL:
        if body.display_name is not None or body.description is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot edit metadata of an OFFICIAL skill",
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
    # An explicit rename must win over a bundle-shipped locale map — the
    # list endpoint resolves ``display_name_i18n`` first, so leaving a
    # stale map in place would silently shadow the edit.
    if "display_name" in updates:
        updates["display_name_i18n"] = None
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
    """Fetch a skill visible to ``user_id`` (own CUSTOM or any OFFICIAL).

    Used by PATCH (OFFICIAL toggle path uses ``user_skill_preference``;
    the row itself is shared) and GET /skills/{id}/download-url — both
    operate on the wider "visible" set than the owner-only
    ``_get_owned_skill``. SYSTEM skills are agent-only, so they resolve
    to 404 here just like a stranger's CUSTOM.
    """
    stmt = select(Skill).where(
        Skill.id == skill_id,
        or_(Skill.user_id == user_id, Skill.origin == SkillOrigin.OFFICIAL),
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
