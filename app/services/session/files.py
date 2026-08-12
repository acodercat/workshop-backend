"""Authorization helpers for the ``session_files`` ownership table.

Pure DB queries — no FastAPI / HTTP concerns. Both the owner-side and
share-side download endpoints translate
:class:`~app.services.session.exceptions.FileNotInSessionError` into
their own 403, which keeps this module reusable from a worker / CLI /
test that has no HTTP envelope to populate.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_files import SessionFile
from app.services.session.exceptions import FileNotInSessionError


async def assert_file_belongs_to_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    file_key: str,
) -> None:
    """Raise :class:`FileNotInSessionError` if ``file_key`` is not
    bound to ``session_id``.

    Single indexed lookup against ``session_files`` (UNIQUE on
    ``file_key`` makes this O(log n))."""
    stmt = (
        select(SessionFile.id)
        .where(SessionFile.session_id == session_id)
        .where(SessionFile.file_key == file_key)
        .limit(1)
    )
    if (await db.execute(stmt)).first() is None:
        raise FileNotInSessionError(f"file_key {file_key!r} is not bound to session {session_id}")
