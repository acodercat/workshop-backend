"""User-uploaded skill bundle CRUD.

Flow mirrors the documents upload pattern: a presigned PUT URL goes
to OSS, the client PUTs bytes directly, then comes back with the
``bundle_key`` to register metadata. Backend fetches the just-
uploaded zip once to extract picker name + description from SKILL.md
and writes the ``user_skill`` row.

The bundle itself is **immutable** through this surface — edits to a
skill require a new upload (gives the worker a clean cache key and
avoids ambiguity about which version a running session staged). The
PATCH endpoint only changes picker-visible metadata (name /
description) and the enable toggle.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.common.filenames import InvalidFilenameError
from app.models.user import User
from app.models.user_skill import UserSkill
from app.rate_limit import limiter
from app.schemas.file import UploadUrlResponse
from app.schemas.user_skill import (
    UserSkillCreate,
    UserSkillResponse,
    UserSkillUpdate,
)
from app.services import file_storage
from app.services.file_storage import (
    FileTooLargeError,
    SKILL_BUNDLE_MAX_BYTES,
    UnsupportedFileTypeError,
    delete_skill_bundle,
    download_skill_bundle,
    head_skill_bundle_size,
)
from app.services.skill_bundle import InvalidSkillBundleError, parse_bundle

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


@router.get("/skills/upload-url", response_model=UploadUrlResponse)
@limiter.limit("30/minute")
async def get_skill_upload_url(
    request: Request,
    filename: str = Query(..., description="Bundle filename (.zip)"),
    size_bytes: int = Query(..., ge=0, description="Declared bundle size in bytes"),
    current_user: User = Depends(authenticate_and_get_user),
) -> UploadUrlResponse:
    """Mint a presigned PUT URL for the frontend to upload a skill
    bundle (.zip) directly to OSS.

    Tighter per-user rate limit than the documents endpoint (30/min vs
    60/min) — skill uploads are a deliberate user action, not part of
    a chat flow, so the lower ceiling absorbs accidental retries
    without throttling normal use.
    """
    try:
        return file_storage.generate_skill_upload_url(
            filename=filename,
            size_bytes=size_bytes,
            user_id=current_user.id,
        )
    except (InvalidFilenameError, UnsupportedFileTypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        )


@router.post(
    "/skills",
    response_model=UserSkillResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("30/minute")
async def register_skill(
    request: Request,
    body: UserSkillCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserSkill:
    """Register a previously-uploaded bundle. Backend pulls the zip
    from OSS, parses SKILL.md, validates structure, and writes the
    ``user_skill`` row.

    400 if the zip is malformed, lacks SKILL.md, or has unsafe entries.
    409 if the extracted skill name collides with one this user
        already has — the user must rename in their SKILL.md and
        re-upload (the name uniqueness is per-user, not global).
    """
    # Authorize: the bundle_key the client passes must be one this user
    # could have uploaded. Keys are minted as
    # ``user_skills/{user_id}/{uuid}/...`` so a prefix check is the
    # whole story — no DB lookup needed because we haven't written
    # the row yet (chicken-and-egg avoidance).
    expected_prefix = f"user_skills/{current_user.id}/"
    if not body.bundle_key.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="bundle_key does not belong to the authenticated user",
        )

    # head_object → size sanity check (defends against a client that
    # lied at upload-URL time and PUT more bytes than declared).
    try:
        actual_size = await asyncio.to_thread(
            head_skill_bundle_size, body.bundle_key,
        )
    except Exception as exc:
        # OSS 404 looks like NoSuchKey from the SDK; treat any retrieval
        # failure as "bundle not actually uploaded" to keep the surface
        # small (an actually-transient OSS error → user re-tries).
        logger.warning("skill register: OSS head failed for %s: %s", body.bundle_key, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bundle not found in storage; upload it first",
        )
    if actual_size > SKILL_BUNDLE_MAX_BYTES:
        # OSS doesn't honor signed Content-Length so a misbehaving
        # client can PUT past the upload-URL cap — re-check at register
        # time before we read bytes into memory.
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"bundle is {actual_size} bytes; limit is {SKILL_BUNDLE_MAX_BYTES}",
        )

    data = await asyncio.to_thread(download_skill_bundle, body.bundle_key)
    try:
        parsed = parse_bundle(data)
    except InvalidSkillBundleError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )

    skill = UserSkill(
        user_id=current_user.id,
        name=parsed.name,
        description=parsed.description,
        bundle_key=body.bundle_key,
        bundle_size=actual_size,
    )
    db.add(skill)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a skill named {parsed.name!r} already exists for this user — "
                f"change the name in SKILL.md frontmatter and re-upload, or "
                f"delete the existing one first"
            ),
        )
    await db.refresh(skill)
    logger.info(
        "skill registered: id=%s user_id=%s name=%r size=%d",
        skill.id, current_user.id, skill.name, skill.bundle_size,
    )
    return skill


@router.get("/skills", response_model=list[UserSkillResponse])
async def list_skills(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> list[UserSkill]:
    """Return all skills owned by the authenticated user, newest first.

    Unpaginated — per-user skill count is expected to be small (tens
    at most); add pagination only if real workloads hit that.
    """
    stmt = (
        select(UserSkill)
        .where(UserSkill.user_id == current_user.id)
        .order_by(UserSkill.created_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/skills/{skill_id}", response_model=UserSkillResponse)
async def update_skill(
    skill_id: int,
    body: UserSkillUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> UserSkill:
    """Edit picker-visible metadata or flip the enable toggle. The
    bundle itself is immutable — re-upload for content changes."""
    skill = await _get_owned_skill(db, skill_id, current_user.id)

    if body.name is not None:
        skill.name = body.name
    if body.description is not None:
        skill.description = body.description
    if body.enabled is not None:
        skill.enabled = body.enabled

    # Only the ``(user_id, name)`` unique constraint can fire here,
    # and only when ``body.name`` is set — let other IntegrityErrors
    # surface as 500 (they'd indicate a genuine bug, not user input).
    if body.name is not None:
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a skill named {body.name!r} already exists for this user",
            )
    else:
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
    await db.delete(skill)
    await db.commit()

    try:
        await asyncio.to_thread(delete_skill_bundle, bundle_key)
    except Exception as exc:
        # Don't fail the request — the DB row is gone, which is the
        # user-visible state. Leftover OSS object is cleanup-cron's job.
        logger.warning(
            "skill delete: OSS cleanup failed for %s (skill id=%s): %s",
            bundle_key, skill_id, exc,
        )


# ── Internal helpers ─────────────────────────────────────────────────────


async def _get_owned_skill(
    db: AsyncSession, skill_id: int, user_id: int,
) -> UserSkill:
    """Fetch a skill and assert it belongs to ``user_id``.

    Combining the lookup with the ownership check on one round-trip
    means a 404 to the caller covers both "doesn't exist" and "exists
    but belongs to someone else" — denies the existence-oracle attack
    vector by default.
    """
    stmt = select(UserSkill).where(
        UserSkill.id == skill_id, UserSkill.user_id == user_id,
    )
    result = await db.execute(stmt)
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="skill not found",
        )
    return skill
