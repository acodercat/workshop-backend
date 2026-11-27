"""Pinning a session: where it sits in its owner's list.

Pinned sessions list first, the most recently pinned on top
(``GET /sessions``). Pinning is not activity: ``PATCH /sessions/{id}`` keeps
``session.updated_at`` for a request that only pins or unpins, or an
unpinned session would jump to the top of the recent ones.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import SessionPinLimitError
from app.models.session import Session

# Per-user cap on pinned sessions. The client lists every pinned session
# in a sidebar group that does not scroll, so this bounds how tall it grows.
MAX_PINNED_SESSIONS = 10


async def set_pinned(db: AsyncSession, session: Session, pinned: bool) -> None:
    """Pin or unpin ``session``.

    Pinning an already pinned session keeps its place among the pinned.
    Raises :class:`~app.errors.SessionPinLimitError` past the cap, which is
    race-acceptable like the project caps: two concurrent pins could put the
    user one over.
    """
    if pinned == (session.pinned_at is not None):
        return
    if pinned:
        await _assert_headroom(db, session.user_id)
    session.pinned_at = func.now() if pinned else None


async def _assert_headroom(db: AsyncSession, user_id: int) -> None:
    pinned = await db.scalar(
        select(func.count(Session.id)).where(
            Session.user_id == user_id,
            Session.deleted_at.is_(None),
            Session.pinned_at.is_not(None),
        )
    )
    if pinned is not None and pinned >= MAX_PINNED_SESSIONS:
        raise SessionPinLimitError(MAX_PINNED_SESSIONS)
