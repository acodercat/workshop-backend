"""The name pair the console renders as one operator-facing identity.

Its own module rather than a helper beside either caller: two console
endpoints resolve an owner for a single row (skill edit, feedback
triage) and their list siblings resolve the same thing through a join.
Written out twice it was byte-identical twice, and the pair would drift
the first time one of them started answering differently for a deleted
account.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def owner_identity(db: AsyncSession, user_id: int | None) -> tuple[str | None, str | None]:
    """``(username, display name)`` for one account, or a pair of ``None``.

    One row rather than two scalar reads: the console shows a username
    with the display name under it, and fetching the second separately
    would be a round trip for half of one identity.

    ``None`` for a row that has no owner — an anonymous submission, a
    platform-published skill — which is the same answer the list queries'
    outer joins give, so the single-row path and the page agree.
    """
    if user_id is None:
        return None, None
    row = (
        await db.execute(select(User.username, User.name).where(User.id == user_id))
    ).one_or_none()
    return (row.username, row.name) if row else (None, None)
