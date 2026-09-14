"""Authorization helpers for the ``session_files`` ownership table.

Pure DB queries — no FastAPI / HTTP concerns. Two questions, asked at two
moments:

- **May this session read this file?** (:func:`assert_file_belongs_to_session`)
  Asked on download and preview. Both the owner-side and share-side
  endpoints translate its error into their own 403.
- **May this caller store this file?** (:func:`assert_files_owned_by`)
  Asked when a message binds its attachments, before the key is written.

Keeping both here keeps this module reusable from a worker / CLI / test
that has no HTTP envelope to populate.
"""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_files import SessionFile
from app.services.session.exceptions import FileNotInSessionError, FileNotOwnedError


async def assert_file_belongs_to_session(
    db: AsyncSession, *, session_id: uuid.UUID, file_key: str,
) -> None:
    """Raise :class:`FileNotInSessionError` if ``file_key`` is not
    bound to ``session_id``.

    Single indexed lookup: ``UNIQUE(session_id, file_key)`` covers both
    filters."""
    stmt = (
        select(SessionFile.id)
        .where(SessionFile.session_id == session_id)
        .where(SessionFile.file_key == file_key)
        .limit(1)
    )
    if (await db.execute(stmt)).first() is None:
        raise FileNotInSessionError(
            f"file_key {file_key!r} is not bound to session {session_id}"
        )


async def assert_files_owned_by(
    db: AsyncSession,
    *,
    user_id: int | None,
    file_keys: Collection[str],
) -> None:
    """Raise :class:`FileNotOwnedError` if any key is held by someone else.

    The one definition of "may this caller reference this upload". It is
    load-bearing: a share viewer legitimately holds the ``file_key`` of a
    file in the session shared with them, and a path that let them store
    it would let them keep minting download URLs after the share was
    revoked. The same owner binding their own file to another of their
    sessions is allowed.

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
        .limit(1)
    )
    foreign = (await db.execute(held_by_someone_else)).scalar_one_or_none()
    if foreign is not None:
        raise FileNotOwnedError(f"file_key {foreign!r} belongs to another user")
