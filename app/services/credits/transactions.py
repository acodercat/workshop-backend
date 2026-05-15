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

**Pagination is cursor-based** (Stripe / OpenAI / GitHub pattern):
caller passes ``before`` (an ISO timestamp) to fetch rows older than
that point, and gets back ``next_cursor`` for the following page.
This pattern beats LIMIT/OFFSET on three counts that matter as the
log grows: deep-page reads stay O(log n) instead of O(offset),
concurrent inserts can't shift the page window, and we drop the
``SELECT count(*)`` round-trip entirely. The single ordering key
``at DESC`` is unambiguous per user (microsecond-resolution
timestamps + single-threaded writes per user — see callers in
:mod:`app.services.credits`), so no tiebreaker column is needed.

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


# Two non-obvious bits below earn comments; the rest of the SQL
# reads straight.
#
# 1. The ``welcome-%`` LIKE pattern retags ``apply_topup`` rows
#    whose idempotency key starts with ``welcome-`` as
#    ``WELCOME_GRANT`` — registration onboarding is the only
#    producer of that prefix.
#
# 2. ``CAST(:before_at AS timestamptz)`` is load-bearing. Postgres
#    can't infer the type of a NULL bind from
#    ``$x IS NULL OR col < $x`` alone (``IS NULL`` is polymorphic,
#    ``<`` is overloaded), so without the cast the first page
#    (``before_at = None``) raises ``AmbiguousParameter``.
_TRANSACTIONS_SQL = text(
    """
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
          AND (CAST(:before_at AS timestamptz) IS NULL
               OR cr.created_at < CAST(:before_at AS timestamptz))
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
          AND (CAST(:before_at AS timestamptz) IS NULL
               OR cl.created_at < CAST(:before_at AS timestamptz))
    )
    SELECT kind, amount, at, ref
    FROM (
        SELECT * FROM reservation_rows
        UNION ALL
        SELECT * FROM direct_rows
    ) all_rows
    ORDER BY at DESC
    LIMIT :limit
    """
)


async def list_transactions(
    db: AsyncSession,
    *,
    user_id: int,
    before_at: datetime | None,
    limit: int,
) -> list[TransactionRow]:
    """Return one newest-first page of user-facing transactions.

    Pass ``before_at=None`` for the first page; pass the previous
    page's oldest ``at`` for subsequent pages. The endpoint layer
    shapes ``next_cursor`` from ``len(rows) == limit``.
    """
    rows = (await db.execute(
        _TRANSACTIONS_SQL,
        {"user_id": user_id, "before_at": before_at, "limit": limit},
    )).all()
    return [
        TransactionRow(
            kind=TransactionKind(row.kind),
            amount=int(row.amount),
            at=row.at,
            ref=row.ref or {},
        )
        for row in rows
    ]
