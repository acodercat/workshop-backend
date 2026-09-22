"""Who may use an uploaded ``file_key``.

Two questions, both answered from the tables that record a binding:
whether a key is bound to a given session (the download and preview
endpoints ask), and whether anyone other than the caller holds a key they
are about to store (every write path asks — a message attachment and a
schedule attachment alike).

Pure DB queries — no FastAPI / HTTP concerns. Both raise
:class:`~app.errors.FileNotInSessionError` and
:class:`~app.errors.FileNotOwnedError` respectively — typed application
errors the shared handler renders as 403s — so the module works the same from a
worker or a test with no HTTP envelope.
"""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import FileNotInSessionError, FileNotOwnedError
from app.models.schedule_file import ScheduleFile
from app.models.session_files import SessionFile


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
    user_id: int | None,
    file_keys: Collection[str],
) -> None:
    """Raise :class:`FileNotOwnedError` if any key is held by someone else.

    The one definition of "may this caller reference this upload", read by
    every path that stores a ``file_key`` on the caller's behalf — a message
    attachment and a schedule attachment alike. It is load-bearing: a share
    viewer legitimately holds the ``file_key`` of a file in the session
    shared with them, and a path that let them store it would let them keep
    minting download URLs after the share was revoked.

    "Held" means either table. A schedule attachment lives in
    ``schedule_file`` from the moment it is saved and only reaches
    ``session_files`` when the task first fires, which may be never for a
    paused one — so a check that read only ``session_files`` would treat it
    as unbound indefinitely, and the first stranger to learn the key could
    bind it and lock its real owner out of their own attachment.

    A key nobody holds passes. It is only knowable by whoever minted the
    presigned upload, and refusing it would refuse every fresh upload.

    ``IS DISTINCT FROM`` rather than ``!=`` for one reason: rows whose owner
    is NULL. ``user_id != 7`` is unknown for such a row and drops it, so a
    logged-in caller would pass on a key held anonymously; ``IS DISTINCT
    FROM 7`` keeps it, and the check fails closed. (The anonymous-caller
    case is *not* the reason — SQLAlchemy already compiles ``!= None`` to
    ``IS NOT NULL``.)
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
            select(ScheduleFile.file_key).where(
                ScheduleFile.file_key.in_(file_keys),
                ScheduleFile.user_id.is_distinct_from(user_id),
            )
        )
        .limit(1)
    )
    foreign = (await db.execute(held_by_someone_else)).scalar_one_or_none()
    if foreign is not None:
        raise FileNotOwnedError(f"file_key {foreign!r} belongs to another user")
