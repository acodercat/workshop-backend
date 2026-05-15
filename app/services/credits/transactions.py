"""User-facing transaction view over the raw ledger.

The raw ``credit_ledger`` records every state change as a separate
row (``RESERVE``, then ``SETTLE`` × 2 or ``REFUND`` × 1), which is
audit-correct but confusing to read in a settings UI. This module
collapses each turn's ledger trail into **one** user-facing
transaction:

- ``credit_reservation`` is the spine — its ``state`` + ``reserved``
  fields tell us the net balance impact for that turn without
  joining all the ledger rows. We only join ledger when we need the
  actual cost (the negative ``SETTLE`` row for ``SETTLED`` state).
- Non-reservation ledger rows (``TOPUP`` / ``ADJUST``) pass through
  directly. ``TOPUP`` rows whose idempotency key starts with
  ``welcome-`` are re-tagged as ``WELCOME_GRANT`` so the UI can
  surface the onboarding moment cleanly.

**Pagination is offset-based** (``page`` / ``page_size`` + ``total``),
matching the project-wide convention used by ``GET /sessions`` and
:mod:`app.common.pagination`. The activity list is bounded per user
and read at human scroll speed, so the simplicity + a consistent
``PaginatedResponse`` shape across the API outweighs deep-page
offset cost here.

The endpoint :func:`~app.api.endpoints.credits.get_credits_transactions`
calls into here. The raw ``GET /credits`` endpoint still returns
unprocessed ledger rows for audit / debugging.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class TransactionKind(StrEnum):
    """User-facing transaction labels.

    Two families:

    - **Turn-related** (derived from ``credit_reservation.state``):
      ``TURN_USED`` (settled with non-zero actual cost),
      ``TURN_REFUND`` (cancelled, full refund),
      ``TURN_PENDING`` (still open — the agent is mid-flight).
    - **Direct** (pass-through from ``credit_ledger``):
      ``WELCOME_GRANT`` (auto-grant at registration),
      ``TOPUP`` (admin-initiated or future payment-driven),
      ``ADJUST`` (manual correction).
    """

    TURN_USED = "TURN_USED"
    TURN_REFUND = "TURN_REFUND"
    TURN_PENDING = "TURN_PENDING"
    WELCOME_GRANT = "WELCOME_GRANT"
    TOPUP = "TOPUP"
    ADJUST = "ADJUST"


class TransactionRow(NamedTuple):
    """One row of the user-facing transaction view.

    ``amount`` is signed µ-credits: positive for credit-in, negative
    for credit-out, zero for ``TURN_REFUND`` (the round trip nets
    out). Callers convert to whole credits at the wire boundary.
    """

    kind: TransactionKind
    amount: int
    at: datetime
    ref: dict[str, Any]


# Offset pagination, matching the project convention (see
# ``GET /sessions`` + ``app/common/pagination.py``): a count query
# and a page query share the same filtered ``all_rows`` CTE so the
# total can't drift from the page. The CTE body is defined once and
# spliced into both statements rather than duplicated.
#
# The only non-obvious bit: the ``welcome-%`` LIKE pattern retags
# ``apply_topup`` rows whose idempotency key starts with ``welcome-``
# as ``WELCOME_GRANT`` — registration onboarding is the sole producer
# of that prefix.
_ROWS_CTE = """
    WITH reservation_rows AS (
        SELECT
            cr.created_at AS at,
            CASE cr.state
                WHEN 'SETTLED'   THEN 'TURN_USED'
                WHEN 'CANCELLED' THEN 'TURN_REFUND'
                ELSE                  'TURN_PENDING'
            END AS kind,
            CASE cr.state
                WHEN 'SETTLED' THEN COALESCE((
                    SELECT amount FROM credit_ledger
                    WHERE (reference->>'reservation_id')::bigint = cr.id
                      AND kind = 'SETTLE' AND amount < 0
                    LIMIT 1
                ), 0)
                WHEN 'CANCELLED' THEN 0
                ELSE -cr.reserved
            END AS amount,
            jsonb_build_object(
                'reservation_id', cr.id,
                'session_id',     cr.session_id,
                'turn_id',        cr.turn_id
            ) AS ref
        FROM credit_reservation cr
        WHERE cr.user_id = :user_id
    ),
    direct_rows AS (
        SELECT
            cl.created_at AS at,
            CASE
                WHEN cl.kind = 'TOPUP' AND cl.idempotency_key LIKE 'welcome-%'
                    THEN 'WELCOME_GRANT'
                ELSE cl.kind::text
            END AS kind,
            cl.amount AS amount,
            cl.reference AS ref
        FROM credit_ledger cl
        WHERE cl.user_id = :user_id
          AND cl.kind IN ('TOPUP', 'ADJUST')
    ),
    all_rows AS (
        SELECT * FROM reservation_rows
        UNION ALL
        SELECT * FROM direct_rows
    )
"""

_TRANSACTIONS_COUNT_SQL = text(
    _ROWS_CTE + " SELECT count(*) AS total FROM all_rows"
)

_TRANSACTIONS_PAGE_SQL = text(
    _ROWS_CTE
    + """
    SELECT kind, amount, at, ref
    FROM all_rows
    ORDER BY at DESC
    OFFSET :offset
    LIMIT :limit
    """
)


async def list_transactions(
    db: AsyncSession,
    *,
    user_id: int,
    page: int,
    page_size: int,
) -> tuple[list[TransactionRow], int]:
    """Return one newest-first page of user-facing transactions plus
    the total row count.

    Offset pagination mirroring ``app.common.pagination.paginate``:
    page / page_size below 1 are clamped to 1. The count and page
    queries share ``_ROWS_CTE`` so ``total`` is consistent with the
    page even under concurrent writes.
    """
    safe_page = max(page, 1)
    safe_page_size = max(page_size, 1)
    offset = (safe_page - 1) * safe_page_size

    total = (
        await db.execute(_TRANSACTIONS_COUNT_SQL, {"user_id": user_id})
    ).scalar() or 0
    rows = (await db.execute(
        _TRANSACTIONS_PAGE_SQL,
        {"user_id": user_id, "offset": offset, "limit": safe_page_size},
    )).all()
    return [
        TransactionRow(
            kind=TransactionKind(row.kind),
            amount=int(row.amount),
            at=row.at,
            ref=row.ref or {},
        )
        for row in rows
    ], total
