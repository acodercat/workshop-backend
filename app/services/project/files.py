"""A project's files: adding one, removing one, and reading the current
version of each.

The endpoints call these on a project row they have locked ``FOR UPDATE``
(see ``app/models/project.py``). That lock is what makes the version
allocation and the per-project cap exact rather than race-acceptable: two
adds to one project queue on it. None of these commit; the caller owns the
transaction.

Adding a file mirrors :mod:`app.services.schedule.files`. Ownership is
checked with the predicates a message attachment uses; the stored size is
what OSS reports, never the client's claim, because the upload quota sums
it; and the object is taken off the orphan-reaping rule *before* the
commit, failing the add if storage refuses — a project file may not be
read for weeks, and one still tagged would be deleted under the project in
about a day. That step runs last, after every refusal, because a refusal
after it could not take the tag back.
"""

import uuid

from sqlalchemy import Select, distinct, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.common.filenames import filename_from_key
from app.errors import (
    FileNotInSessionError,
    ProjectFileLimitError,
    ProjectFileNotFoundError,
    SessionNotFoundError,
)
from app.models.project import Project
from app.models.project_file import ProjectFile, ProjectFileOrigin
from app.models.session import Session
from app.models.session_files import SessionFile
from app.schemas.project import ProjectFileCreate, SessionFileRef
from app.services import file_storage
from app.services.session.files import assert_files_owned_by

# Per-project cap on live file *names*. A new version of a name the project
# has is never refused: replacing a file must not need a delete first. Also
# what lets the client read a project's files in one request.
_MAX_FILES_PER_PROJECT = 100


def latest(project_id: uuid.UUID) -> Select[tuple[ProjectFile]]:
    """Each live name's highest version, newest first.

    A query rather than rows, so the endpoint can page it and count it with
    one definition of "the project's files".
    """
    current = (
        select(ProjectFile)
        .where(ProjectFile.project_id == project_id, ProjectFile.deleted_at.is_(None))
        .distinct(ProjectFile.filename)
        .order_by(ProjectFile.filename, ProjectFile.version.desc())
        .subquery()
    )
    row = aliased(ProjectFile, current)
    return select(row).order_by(row.created_at.desc(), row.id.desc())


async def get(db: AsyncSession, project_id: uuid.UUID, file_id: int) -> ProjectFile:
    """A live version of a file in this project.

    Any live version, not only the current one, so a list the client
    loaded before a newer version arrived still downloads what it shows.
    """
    file = await db.scalar(
        select(ProjectFile).where(
            ProjectFile.id == file_id,
            ProjectFile.project_id == project_id,
            ProjectFile.deleted_at.is_(None),
        )
    )
    if file is None:
        raise ProjectFileNotFoundError("Project file not found.")
    return file


async def add(db: AsyncSession, project: Project, body: ProjectFileCreate) -> ProjectFile:
    """Add a file to the project, as a new name or the next version of one.

    Raises the refusals of whichever source it came from —
    :class:`~app.errors.FileNotOwnedError` for an upload someone else
    holds, :class:`~app.errors.SessionNotFoundError` /
    :class:`~app.errors.FileNotInSessionError` for a session file — then
    :class:`~app.errors.ProjectFileLimitError`, and the storage errors of
    :func:`file_storage.confirmed_sizes` and
    :func:`file_storage.clear_orphan_tags`.
    """
    file = await _from_source(db, body, user_id=project.user_id)
    await _assert_headroom(db, project.id, file.filename)
    (file.size_bytes,) = await file_storage.confirmed_sizes(
        file_storage.SESSION_FILE_POLICY, [file.file_key]
    )
    await file_storage.clear_orphan_tags(file_storage.SESSION_FILE_POLICY, [file.file_key])

    file.project_id = project.id
    file.user_id = project.user_id
    file.version = await _next_version(db, project.id, file.filename)
    db.add(file)
    project.updated_at = func.now()
    await db.flush()
    return file


async def remove(db: AsyncSession, project: Project, file: ProjectFile) -> None:
    """Soft-delete every version of the file's name.

    The owner removed the file, not its latest revision: deleting only that
    one would surface the version before it. A file of the same name added
    later numbers on from the highest version ever written.
    """
    await db.execute(
        update(ProjectFile)
        .where(
            ProjectFile.project_id == project.id,
            ProjectFile.filename == file.filename,
            ProjectFile.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
    )
    project.updated_at = func.now()


async def _from_source(db: AsyncSession, body: ProjectFileCreate, *, user_id: int) -> ProjectFile:
    """The new row's identity and origin, from whichever source the body
    names, with that source's ownership checked."""
    if body.upload is not None:
        upload = body.upload
        # A key nobody holds passes: it is a fresh upload, knowable only by
        # whoever minted it.
        await assert_files_owned_by(db, user_id=user_id, file_keys=[upload.file_key])
        return ProjectFile(
            file_key=upload.file_key,
            filename=upload.filename,
            mime_type=upload.mime_type,
            origin=ProjectFileOrigin.UPLOAD,
        )
    assert body.session_file is not None  # ProjectFileCreate admits exactly one source
    bound = await _session_file(db, body.session_file, user_id=user_id)
    return ProjectFile(
        file_key=bound.file_key,
        filename=bound.filename or filename_from_key(bound.file_key),
        mime_type=bound.mime_type,
        origin=ProjectFileOrigin.SESSION_FILE,
        origin_session_id=bound.session_id,
    )


async def _session_file(db: AsyncSession, ref: SessionFileRef, *, user_id: int) -> SessionFile:
    """The binding of ``ref.file_key`` to one of the caller's live sessions.

    Two refusals, in the order the download endpoint gives them: a session
    the caller cannot see is a 404, and a key not bound to it a 403 — so a
    probe cannot use somebody else's key against a session it owns.
    """
    session_is_theirs = await db.scalar(
        select(
            exists().where(
                Session.id == ref.session_id,
                Session.user_id == user_id,
                Session.deleted_at.is_(None),
            )
        )
    )
    if not session_is_theirs:
        raise SessionNotFoundError("Session not found.")
    bound = await db.scalar(
        select(SessionFile)
        .where(SessionFile.session_id == ref.session_id, SessionFile.file_key == ref.file_key)
        .limit(1)
    )
    if bound is None:
        raise FileNotInSessionError(
            f"file_key {ref.file_key!r} is not bound to session {ref.session_id}"
        )
    return bound


async def _assert_headroom(db: AsyncSession, project_id: uuid.UUID, filename: str) -> None:
    """Refuse a new name in a project already at the cap; never a new
    version of a name it has."""
    live = (ProjectFile.project_id == project_id) & ProjectFile.deleted_at.is_(None)
    if await db.scalar(select(exists().where(live, ProjectFile.filename == filename))):
        return
    names = await db.scalar(select(func.count(distinct(ProjectFile.filename))).where(live))
    if names is not None and names >= _MAX_FILES_PER_PROJECT:
        raise ProjectFileLimitError(_MAX_FILES_PER_PROJECT)


async def _next_version(db: AsyncSession, project_id: uuid.UUID, filename: str) -> int:
    """The version a new row of this name takes: one past the highest ever
    written, deleted ones included, so the unique constraint never collides.

    The allocation rule ``app/models/project_file.py`` states for both
    writers; correct only under the project's row lock.
    """
    highest = await db.scalar(
        select(func.max(ProjectFile.version)).where(
            ProjectFile.project_id == project_id,
            ProjectFile.filename == filename,
        )
    )
    return (highest or 0) + 1
