"""Aggregate reporting over the ``user`` table.

Its own module for the reason :func:`app.services.check_in.count_in_window`
is: the console's dashboard asks how many people arrived, and that is a
question about the account table, not about the ledger. Folding it into
``/stats/credits`` would make the credits module answer for a table it
does not own.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def count_registered_since(db: AsyncSession, *, since: datetime) -> int:
    """How many accounts were created since ``since``.

    Every account, whatever its status: a registration that later got
    disabled still happened, and a growth figure that quietly drops them
    would disagree with the invite redemptions that produced them.
    """
    return int(
        await db.scalar(select(func.count()).select_from(User).where(User.created_at >= since)) or 0
    )
