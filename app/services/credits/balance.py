"""Read the cached balance (µ-credits) for a user."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_balance(db: AsyncSession, user_id: int) -> int:
    """Return current ``user.credit_balance`` in µ-credits.

    Caller-relevant note: this reads the cached column directly. Any
    in-flight ``OPEN`` reservation already debited the balance, so the
    returned number is "spendable" — what the user can still reserve.
    """
    result = await db.execute(
        select(User.credit_balance).where(User.id == user_id)
    )
    balance = result.scalar_one_or_none()
    if balance is None:
        raise ValueError(f"User {user_id} not found")
    return balance
