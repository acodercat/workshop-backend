"""Project CRUD, and a project's files.

A project groups sessions under shared instructions and files. Which sessions
belong to one is ``session.project_id``, set through the session endpoints
(``POST /sessions``, ``PATCH /sessions/{id}``) and listed through
``GET /sessions?project_id=``; this surface manages the project itself and
the files it shares. The rules for those live in
:mod:`app.services.project.files`.

Every write that changes what a project holds locks its row first
(``project_ownership.require_owned_project(for_update=True)``) — see
``app/models/project.py``
for why that one order is enough.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import ColumnElement, Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.common.pagination import paginate
from app.errors import ProjectLimitError
from app.models.project import Project
from app.models.session import Session
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.file import DownloadUrlResponse
from app.schemas.project import (
    ProjectCreate,
    ProjectFileCreate,
    ProjectFileResponse,
    ProjectResponse,
    ProjectUpdate,
)
from app.services import file_storage
from app.services.project import files as project_files, ownership as project_ownership
from config import settings

router = APIRouter(tags=["projects"])

# Per-user cap on live projects. Bounds rows, and the client's one-request
# read of the whole list: the sidebar, the move-to-project menu and a
# session's breadcrumb all look projects up in it.
_MAX_PROJECTS_PER_USER = 100


def _live_projects(user_id: int) -> ColumnElement[bool]:
    """The caller's live projects — what the list shows and what the cap
    counts, so the two cannot disagree about which projects there are."""
    return (Project.user_id == user_id) & Project.deleted_at.is_(None)


async def _assert_headroom(db: AsyncSession, user_id: int) -> None:
    """Refuse a project the caller has no room for.

    Race-acceptable, like the schedule caps: two concurrent creates could
    each pass and put the user one over, which is not worth a row lock for a
    soft policy limit.
    """
    total = await db.scalar(select(func.count(Project.id)).where(_live_projects(user_id)))
    if total is not None and total >= _MAX_PROJECTS_PER_USER:
        raise ProjectLimitError(_MAX_PROJECTS_PER_USER)


def _moved_out(project_id: uuid.UUID) -> Update:
    """Move every session pointing at a project out of it.

    Soft-deleted sessions too, so nothing is left pointing at a deleted
    project. ``updated_at`` is set to itself because ``onupdate`` would
    otherwise stamp ``now()``: being moved out is not activity, and would
    lift every one of these sessions to the top of the recent list.
    """
    return (
        update(Session)
        .where(Session.project_id == project_id)
        .values(project_id=None, updated_at=Session.updated_at)
    )


@router.post("/projects", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_project(
    request: Request,
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ProjectResponse:
    """Create a project owned by the caller. 403 at the per-account cap."""
    await _assert_headroom(db, current_user.id)
    project = Project(
        user_id=current_user.id,
        name=body.name,
        instructions=body.instructions,
        icon=body.icon,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("/projects")
async def list_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[ProjectResponse]:
    """The caller's live projects, most recently updated first."""
    live = _live_projects(current_user.id)
    base = select(Project).where(live).order_by(Project.updated_at.desc())
    total = (await db.execute(select(func.count(Project.id)).where(live))).scalar() or 0
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return PaginatedResponse(
        items=[ProjectResponse.model_validate(project) for project in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/projects/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ProjectResponse:
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id
    )
    return ProjectResponse.model_validate(project)


@router.patch("/projects/{project_id}")
@limiter.limit("30/minute")
async def update_project(
    request: Request,
    project_id: uuid.UUID,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ProjectResponse:
    """Rename a project, replace its instructions or icon, or pin or unpin
    it. ``None`` leaves a field alone.

    Re-pinning a pinned project keeps its place among the pinned.
    """
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id
    )
    if body.name is not None:
        project.name = body.name
    if body.instructions is not None:
        project.instructions = body.instructions
    if body.icon is not None:
        project.icon = body.icon
    if body.pinned is not None and body.pinned != (project.pinned_at is not None):
        project.pinned_at = func.now() if body.pinned else None
    if body.model_fields_set == {"pinned"}:
        # Pinning is not a change to the project. Set to itself, or
        # ``onupdate`` would stamp now() and lift it to the top of the list.
        project.updated_at = Project.updated_at
    await db.commit()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_project(
    request: Request,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Soft-delete a project and move its sessions out of it.

    The sessions are kept (:func:`_moved_out`): deleting a grouping is not
    discarding the conversations in it, and the owner can still delete
    those one by one.

    The project's files go with it: their rows stay, unreadable behind the
    deleted project, and their OSS objects stay too, as a deleted session's
    uploads do.

    The project row is locked first, as every write that changes what a
    project holds does, so no session can be created in or moved into it
    while it is being emptied.
    """
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id, for_update=True
    )
    await db.execute(_moved_out(project.id))
    project.deleted_at = func.now()
    await db.commit()


@router.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> PaginatedResponse[ProjectFileResponse]:
    """The current version of each of the project's files, newest first."""
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id
    )
    current = project_files.latest(project.id)
    total = await db.scalar(select(func.count()).select_from(current.subquery())) or 0
    rows = (await db.execute(paginate(current, page, page_size))).scalars().all()
    return PaginatedResponse(
        items=[ProjectFileResponse.model_validate(project_file) for project_file in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.post("/projects/{project_id}/files", status_code=status.HTTP_201_CREATED)
# The upload-URL mint's limit, not the 30/minute of other writes: each file
# uploaded on the project's page is one mint and then one add, so a lower
# limit here would refuse the second half of uploads the first half took —
# leaving objects in storage that the project never got.
@limiter.limit(settings.rate_limit_upload_url)
async def add_project_file(
    request: Request,
    project_id: uuid.UUID,
    body: ProjectFileCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ProjectFileResponse:
    """Add an upload or one of the caller's session files to the project.

    A name the project already has becomes its next version. The project
    is locked for the whole add, which includes two round trips to object
    storage; a concurrent add to the same project waits that long, which
    is the price of an exact version and cap.
    """
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id, for_update=True
    )
    file = await project_files.add(db, project, body)
    await db.commit()
    await db.refresh(file)
    return ProjectFileResponse.model_validate(file)


@router.get(
    "/projects/{project_id}/files/{file_id}/download-url",
    response_model=DownloadUrlResponse,
)
@limiter.limit("60/minute")
async def get_project_file_download_url(
    request: Request,
    project_id: uuid.UUID,
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> DownloadUrlResponse:
    """Mint a presigned GET URL for a project file, the same shape a session
    file's download URL has."""
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id
    )
    file = await project_files.get(db, project.id, file_id)
    url, expires_in = file_storage.generate_download_url(
        file_storage.SESSION_FILE_POLICY, file.file_key, file.filename
    )
    return DownloadUrlResponse(download_url=url, file_key=file.file_key, expires_in=expires_in)


@router.delete(
    "/projects/{project_id}/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit("30/minute")
async def delete_project_file(
    request: Request,
    project_id: uuid.UUID,
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> None:
    """Remove a file — every version of its name — from the project.

    The OSS object stays, as a deleted session's uploads do: another
    session or schedule may bind the same key.
    """
    project = await project_ownership.require_owned_project(
        db, user_id=current_user.id, project_id=project_id, for_update=True
    )
    file = await project_files.get(db, project.id, file_id)
    await project_files.remove(db, project, file)
    await db.commit()
