"""Project CRUD.

A project groups sessions under shared instructions and files. Which sessions
belong to one is ``session.project_id``, set through the session endpoints
(``POST /sessions``, ``PATCH /sessions/{id}``) and listed through
``GET /sessions?project_id=``; this surface manages the project itself.

Every write that changes what a project holds locks its row first — see
``app/models/project.py`` for why that one order is enough.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.common.pagination import paginate
from app.errors import ProjectLimitError, ProjectNotFoundError
from app.models.project import Project
from app.models.session import Session
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.collection_response import PaginatedResponse, PaginationMeta
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate

router = APIRouter(tags=["projects"])

# Per-user cap on live projects. Bounds rows, and the client's one-request
# read of the whole list: the sidebar, the move-to-project menu and a
# session's breadcrumb all look projects up in it.
_MAX_PROJECTS_PER_USER = 100


async def _get_or_404(
    db: AsyncSession, project_id: uuid.UUID, user_id: int, *, for_update: bool = False
) -> Project:
    """Load a live project the caller owns.

    Ownership and liveness are folded into the lookup, so someone else's
    project, a deleted one and a nonexistent one are the same 404.

    ``for_update`` takes the project's lock — the first step of every write
    that changes what the project holds.
    """
    query = select(Project).where(
        Project.id == project_id,
        Project.user_id == user_id,
        Project.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    project = (await db.execute(query)).scalar_one_or_none()
    if project is None:
        raise ProjectNotFoundError("Project not found.")
    return project


async def _assert_headroom(db: AsyncSession, user_id: int) -> None:
    """Refuse a project the caller has no room for.

    Race-acceptable, like the schedule caps: two concurrent creates could
    each pass and put the user one over, which is not worth a row lock for a
    soft policy limit.
    """
    total = await db.scalar(
        select(func.count(Project.id)).where(
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
        )
    )
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
    project = Project(user_id=current_user.id, name=body.name, instructions=body.instructions)
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
    filter_clause = (Project.user_id == current_user.id) & Project.deleted_at.is_(None)
    base = select(Project).where(filter_clause).order_by(Project.updated_at.desc())
    total = (await db.execute(select(func.count(Project.id)).where(filter_clause))).scalar() or 0
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return PaginatedResponse(
        items=[ProjectResponse.model_validate(p) for p in rows],
        meta=PaginationMeta(total=total, page_size=page_size, page=page),
    )


@router.get("/projects/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> ProjectResponse:
    project = await _get_or_404(db, project_id, current_user.id)
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
    """Rename a project, or replace its instructions. ``None`` leaves a field
    alone."""
    project = await _get_or_404(db, project_id, current_user.id)
    if body.name is not None:
        project.name = body.name
    if body.instructions is not None:
        project.instructions = body.instructions
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
    project = await _get_or_404(db, project_id, current_user.id, for_update=True)
    await db.execute(_moved_out(project.id))
    project.deleted_at = func.now()
    await db.commit()
