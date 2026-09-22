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
- **Refunded turns are omitted.** A ``CANCELLED`` reservation is a
  full reserve→refund round-trip that nets to zero — the customer was
  never charged (a voided authorization hold, not a refund), so a
  zero-amount row only reads as "money vanished". These are filtered
  out; the ``RESERVE`` / ``REFUND`` ledger pair stays on the audit
  trail, just off the user-facing list.
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

The endpoint :func:`~app.api.public.credits.get_credits_transactions`
calls into here.
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
      ``TURN_USED`` (settled with the actual cost debited),
      ``TURN_PENDING`` (still open — the agent is mid-flight). A
      refunded (``CANCELLED``) reservation produces **no** row — it
      netted to zero and is filtered out (see the module docstring).
    - **Direct** (pass-through from ``credit_ledger``):
      ``WELCOME_GRANT`` (auto-grant at registration),
      ``TOPUP`` (admin-initiated or future payment-driven),
      ``ADJUST`` (manual correction),
      ``REFERRAL`` (reward for a successful referral),
      ``CHECKIN`` (daily check-in reward).
    """

    TURN_USED = "TURN_USED"
    TURN_PENDING = "TURN_PENDING"
    WELCOME_GRANT = "WELCOME_GRANT"
    TOPUP = "TOPUP"
    ADJUST = "ADJUST"
    REFERRAL = "REFERRAL"
    CHECKIN = "CHECKIN"


class TransactionRow(NamedTuple):
    """One row of the user-facing transaction view.

    ``amount`` is signed µ-credits: positive for credit-in, negative
    for credit-out. Callers convert to whole credits at the wire
    boundary.
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
# ``tie_source`` / ``tie_id`` exist only to make the page query's sort a
# total order; nothing selects them.
#
# The only non-obvious bit: the ``welcome-%`` LIKE pattern retags
# ``apply_topup`` rows whose idempotency key starts with ``welcome-``
# as ``WELCOME_GRANT`` — registration onboarding is the sole producer
# of that prefix.
_ROWS_CTE = """
    WITH reservation_rows AS (
        SELECT
            cr.created_at AS at,
            -- Ordering only, never projected: see the ORDER BY below.
            'reservation' AS tie_source,
            cr.id AS tie_id,
            CASE cr.state
                WHEN 'SETTLED' THEN 'TURN_USED'
                ELSE                'TURN_PENDING'
            END AS kind,
            CASE cr.state
                WHEN 'SETTLED' THEN COALESCE((
                    SELECT amount FROM credit_ledger
                    WHERE reservation_id = cr.id
                      AND kind = 'SETTLE' AND amount < 0
                    LIMIT 1
                ), 0)
                ELSE -cr.reserved
            END AS amount,
            jsonb_build_object(
                'reservation_id', cr.id,
                'session_id',     cr.session_id,
                'turn_id',        cr.turn_id
            ) AS ref
        FROM credit_reservation cr
        WHERE cr.user_id = :user_id
          -- Refunded turns net to zero → omit (see module docstring).
          AND cr.state <> 'CANCELLED'
    ),
    direct_rows AS (
        SELECT
            cl.created_at AS at,
            'ledger' AS tie_source,
            cl.id AS tie_id,
            CASE
                WHEN cl.kind = 'TOPUP' AND cl.idempotency_key LIKE 'welcome-%'
                    THEN 'WELCOME_GRANT'
                ELSE cl.kind::text
            END AS kind,
            cl.amount AS amount,
            cl.reference AS ref
        FROM credit_ledger cl
        WHERE cl.user_id = :user_id
          AND cl.kind IN ('TOPUP', 'ADJUST', 'REFERRAL', 'CHECKIN')
    ),
    all_rows AS (
        SELECT * FROM reservation_rows
        UNION ALL
        SELECT * FROM direct_rows
    )
"""

_TRANSACTIONS_COUNT_SQL = text(_ROWS_CTE + " SELECT count(*) AS total FROM all_rows")

_TRANSACTIONS_PAGE_SQL = text(
    _ROWS_CTE
    + """
    SELECT kind, amount, at, ref
    FROM all_rows
    -- ``at`` alone is not a total order, and OFFSET paging over a sort
    -- whose keys tie is free to order the tied rows differently on each
    -- request — the same row on two pages, or on none. Both source
    -- tables default ``created_at`` to ``now()``, which in Postgres is
    -- the *transaction* timestamp, so a registration that writes a
    -- welcome grant and a referral bonus together gives them the same
    -- ``at`` to the microsecond. The two ids come from different
    -- sequences and can collide, hence the source name first.
    ORDER BY at DESC, tie_source, tie_id DESC
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

    total = (await db.execute(_TRANSACTIONS_COUNT_SQL, {"user_id": user_id})).scalar() or 0
    rows = (
        await db.execute(
            _TRANSACTIONS_PAGE_SQL,
            {"user_id": user_id, "offset": offset, "limit": safe_page_size},
        )
    ).all()
    return [
        TransactionRow(
            kind=TransactionKind(row.kind),
            amount=int(row.amount),
            at=row.at,
            ref=row.ref or {},
        )
        for row in rows
    ], total
