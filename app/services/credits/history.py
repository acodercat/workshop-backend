"""Read paginated ledger history for one user (audit / UI surface)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.pagination import paginate
from app.models.credit_ledger import CreditLedger


async def list_history(
    db: AsyncSession,
    *,
    user_id: int,
    page: int,
    page_size: int,
) -> tuple[list[CreditLedger], int]:
    """Return one page of ledger rows + total row count for ``user_id``.

    Newest first. Uses the ``(user_id, created_at DESC)`` partial-free
    index; cost bounded by ``page_size``.
    """
    filter_clause = CreditLedger.user_id == user_id

    total = await db.scalar(
        select(func.count()).select_from(CreditLedger).where(filter_clause)
    ) or 0

    base = (
        select(CreditLedger)
        .where(filter_clause)
        .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
    )
    result = await db.execute(paginate(base, page, page_size))
    return list(result.scalars().all()), int(total)
