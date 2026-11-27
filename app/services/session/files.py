"""Who may use an uploaded ``file_key``, and how much a user may upload.

Three questions, all answered from the tables that record a binding:
whether a key is bound to a given session (the download and preview
endpoints ask), whether anyone other than the caller holds a key they
are about to store (every write path asks — a message attachment and a
project file alike), and whether one more upload fits the user's quota
(minting an upload URL asks).

Pure DB queries — no FastAPI / HTTP concerns. Each refusal is a typed
application error the shared handler renders, so the module works the same
from a worker or a test with no HTTP envelope.
"""

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    FileCountQuotaExceededError,
    FileNotInSessionError,
    FileNotOwnedError,
    FileSizeQuotaExceededError,
)
from app.models.project import Project
from app.models.project_file import ProjectFile
from app.models.session import Session
from app.models.session_files import SessionFile, SessionFileOrigin
from config import settings


async def assert_file_belongs_to_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    file_key: str,
) -> None:
    """Raise :class:`FileNotInSessionError` if ``file_key`` is not
    bound to ``session_id``.

    Single indexed lookup: ``uq_session_files_session_id_file_key`` leads
    with ``session_id`` and covers both columns."""
    stmt = (
        select(SessionFile.id)
        .where(SessionFile.session_id == session_id)
        .where(SessionFile.file_key == file_key)
        .limit(1)
    )
    if (await db.execute(stmt)).first() is None:
        raise FileNotInSessionError(f"file_key {file_key!r} is not bound to session {session_id}")


async def assert_files_owned_by(
    db: AsyncSession,
    *,
    user_id: int,
    file_keys: Collection[str],
) -> None:
    """Raise :class:`FileNotOwnedError` if any key is held by someone else.

    The one definition of "may this caller reference this upload", read by
    every path that stores a ``file_key`` on the caller's behalf — a message
    attachment and a project file alike. It is load-bearing: a share
    viewer legitimately holds the ``file_key`` of a file in the session
    shared with them, and a path that let them store it would let them keep
    minting download URLs after the share was revoked.

    "Held" means either table. A project file uploaded on the project's page
    is in ``project_file`` alone; a check that read only ``session_files``
    would treat it as unbound indefinitely, and the first stranger to learn
    the key could bind it and lock its real owner out of their own file.
    Deleted project files still count: the object is still their owner's,
    as a soft-deleted session's uploads are.

    A key nobody holds passes. It is only knowable by whoever minted the
    presigned upload, and refusing it would refuse every fresh upload.

    ``IS DISTINCT FROM`` rather than ``!=`` for rows whose owner is NULL:
    ``user_id != 7`` is unknown for such a row and drops it, so the caller
    would pass on a key someone else's row holds; ``IS DISTINCT FROM 7``
    keeps it, and the check fails closed.
    """
    if not file_keys:
        return
    held_by_someone_else = (
        select(SessionFile.file_key)
        .where(
            SessionFile.file_key.in_(file_keys),
            SessionFile.user_id.is_distinct_from(user_id),
        )
        .union_all(
            select(ProjectFile.file_key).where(
                ProjectFile.file_key.in_(file_keys),
                ProjectFile.user_id.is_distinct_from(user_id),
            ),
        )
        .limit(1)
    )
    foreign = (await db.execute(held_by_someone_else)).scalar_one_or_none()
    if foreign is not None:
        raise FileNotOwnedError(f"file_key {foreign!r} belongs to another user")


async def assert_upload_quota(
    db: AsyncSession,
    *,
    user_id: int,
    incoming_size_bytes: int,
) -> None:
    """Raise :class:`FileSizeQuotaExceededError` or
    :class:`FileCountQuotaExceededError` if this upload would put the user
    over a per-user quota. Two gates, both over the objects counted below, each
    once:

    - **Total size** (primary): summed ``size_bytes`` plus this upload
      must stay under ``max_session_files_bytes_per_user``. This gate
      tracks real OSS storage cost.
    - **Count** (backstop): guards against many-tiny-files row explosion
      that the size gate alone wouldn't catch.

    Only ``UPLOAD`` rows count — files the user uploaded directly.
    ``DELIVERABLE`` (tool outputs) and ``INTERNAL`` (``read_image``
    inputs, office→PDF preview sidecars) are written by the worker with
    no upload-time gate, so counting them here would let heavy agent use
    silently exhaust a user's upload headroom.

    Counted per *object*, not per binding. One upload can be bound to many
    sessions while occupying OSS exactly once, so charging it per binding
    would be billing for storage nobody is using. Every file the user added
    to a project is folded in from ``project_file`` — uploaded on its page,
    or added from a session, which it outlives. Those count while both the
    file and its project are live: deleting either frees the quota, as
    deleting a session does.

    ``size_bytes`` on every row is the size OSS reported when the file was
    bound or attached (``file_storage.confirmed_sizes``), never a client's
    declaration; pre-backfill rows with a null size contribute 0 to the
    sum. Race-acceptable: two concurrent mints
    at the boundary can both pass, leaving the user slightly over — not
    worth a row lock for a soft policy limit.
    """
    bound = (
        select(SessionFile.file_key, SessionFile.size_bytes)
        .join(Session, Session.id == SessionFile.session_id)
        .where(
            Session.user_id == user_id,
            Session.deleted_at.is_(None),
            SessionFile.origin == SessionFileOrigin.UPLOAD,
        )
    )
    shared = (
        select(ProjectFile.file_key, ProjectFile.size_bytes)
        .join(Project, Project.id == ProjectFile.project_id)
        .where(
            ProjectFile.user_id == user_id,
            ProjectFile.deleted_at.is_(None),
            Project.deleted_at.is_(None),
        )
    )
    # ``max`` rather than ``any``: the same object is the same number of
    # bytes wherever it is bound, so this picks that number deterministically
    # instead of leaving it to the planner.
    objects = bound.union_all(shared).subquery()
    per_object = (
        select(
            objects.c.file_key,
            func.max(objects.c.size_bytes).label("size_bytes"),
        )
        .group_by(objects.c.file_key)
        .subquery()
    )
    count, used_bytes = (
        await db.execute(
            select(
                func.count(per_object.c.file_key),
                func.coalesce(func.sum(per_object.c.size_bytes), 0),
            )
        )
    ).one()

    size_cap = settings.max_session_files_bytes_per_user
    if used_bytes + incoming_size_bytes > size_cap:
        raise FileSizeQuotaExceededError(
            f"upload would put the user at "
            f"{(used_bytes + incoming_size_bytes) / 1024**3:.2f} GB of "
            f"uploaded files; per-user limit is {size_cap / 1024**3:.1f} GB"
        )
    if count >= settings.max_session_files_per_user:
        raise FileCountQuotaExceededError(
            f"user already has {count} uploaded files; "
            f"per-user limit is {settings.max_session_files_per_user}"
        )
