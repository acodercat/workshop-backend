"""Which project a caller may use.

Two surfaces name a project: its own endpoints, and the session endpoints
that create a session in one or move a session into one. Both must answer
the same way for a project that is deleted, someone else's, or not there —
so the rule lives here rather than beside either, like
:mod:`app.services.connector.ownership`.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ProjectNotFoundError
from app.models.project import Project


async def require_owned_project(
    db: AsyncSession, *, user_id: int, project_id: uuid.UUID, for_update: bool = False
) -> Project:
    """A live project the caller owns.

    Ownership and liveness are folded into the lookup, so someone else's
    project, a deleted one and a nonexistent one are the same
    :class:`~app.errors.ProjectNotFoundError`, and ids are not leaked.

    ``for_update`` takes the project's lock — the first step of every write
    that changes what the project holds (see ``app/models/project.py``).
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
