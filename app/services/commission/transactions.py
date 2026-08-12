"""User-facing transaction view over the commission ledger.

Simpler than the credit equivalent (:mod:`app.services.credits.transactions`):
the commission ledger has no reservation spine to collapse, so each row
maps to one transaction directly. ``kind`` is the same UPPERCASE
identifier the service layer uses, so the frontend matches on stable
codes and resolves localized labels from its own i18n catalog.

**Pagination is offset-based** (``page`` / ``page_size`` + ``total``),
matching the project-wide convention (``GET /sessions``,
:mod:`app.common.pagination`) and the credit transactions view.
"""

from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.pagination import paginate
from app.models.commission_ledger import CommissionLedger, CommissionLedgerKind


class CommissionTransactionRow(NamedTuple):
    """One row of the user-facing commission transaction view.

    ``amount_fen`` is signed RMB cents: positive for commission-in,
    negative for withdrawals / conversions / clawbacks. ``available_at``
    is set on ``COMMISSION`` rows (when the funds unfreeze), NULL
    otherwise. Callers convert to yuan at the wire boundary.
    """

    kind: CommissionLedgerKind
    amount_fen: int
    at: datetime
    available_at: datetime | None
    ref: dict[str, Any]


async def list_transactions(
    db: AsyncSession,
    *,
    user_id: int,
    page: int,
    page_size: int,
) -> tuple[list[CommissionTransactionRow], int]:
    """Return one newest-first page of ``user_id``'s commission
    transactions plus the total row count.

    Offset pagination via :func:`app.common.pagination.paginate` (page /
    page_size below 1 are clamped to 1). The count and page queries share
    one filter so ``total`` stays consistent with the page.
    """
    filter_clause = CommissionLedger.user_id == user_id
    total = (
        await db.execute(select(func.count(CommissionLedger.id)).where(filter_clause))
    ).scalar() or 0
    base = (
        select(CommissionLedger).where(filter_clause).order_by(CommissionLedger.created_at.desc())
    )
    rows = (await db.execute(paginate(base, page, page_size))).scalars().all()
    return [
        CommissionTransactionRow(
            kind=row.kind,
            amount_fen=row.amount_fen,
            at=row.created_at,
            available_at=row.available_at,
            ref=row.reference or {},
        )
        for row in rows
    ], total
