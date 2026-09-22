"""Credit-ledger reporting — the definition of "spend", and the window.

The load-bearing decision is that spend is the **negative SETTLE** row
and nothing else: a turn also writes ``RESERVE -estimate`` and ``SETTLE
+estimate``, so any looser predicate reports estimates or double-counts.
These tests pin that predicate without a database — the SQL text is
what can silently drift. The business-day window arithmetic they used to
cover moved to ``test_business_time.py`` when it became shared.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import inspect

from app.services.credits import analytics

# --- the spend predicate ------------------------------------------------


def test_spend_counts_only_the_negative_settle_row() -> None:
    # RESERVE is an estimate and SETTLE's positive half merely releases
    # the hold; counting either would overstate consumption.
    assert analytics._SPEND_ROWS == "kind = 'SETTLE' AND amount < 0"


def test_every_spend_query_interpolates_the_shared_predicate() -> None:
    # Totals, the daily series, the model split, the leaderboard and the
    # per-session figure must answer the same question, so each
    # interpolates the constant…
    for query_owner in (
        analytics.summarize,
        analytics.user_activity,
        analytics._daily_spend,
        analytics._spend_by_model,
        analytics.session_spend,
    ):
        assert "{_SPEND_ROWS}" in inspect.getsource(query_owner)

    # …and the *code* spells the predicate exactly once, which is what
    # stops two of them drifting into counting different rows. (The
    # module docstring quotes it too; that's documentation, not a
    # second definition, so it is excluded.)
    code = inspect.getsource(analytics).replace(analytics.__doc__ or "", "", 1)
    assert code.count("kind = 'SETTLE'") == 1


def test_grants_exclude_negative_adjustments() -> None:
    # A clawback is an ADJUST with a negative amount; folding it into
    # "granted" would net it away and hide both numbers.
    assert "amount > 0" in analytics._GRANT_ROWS
    assert analytics._CLAWBACK_ROWS == "kind = 'ADJUST' AND amount < 0"


def test_grants_do_not_include_turn_kinds() -> None:
    for kind in ("RESERVE", "SETTLE", "REFUND"):
        assert kind not in analytics._GRANT_ROWS


# --- sold vs given away -------------------------------------------------


def test_sold_is_keyed_on_the_paid_order_not_the_ledger_kind() -> None:
    # ``apply_topup`` writes TOPUP for the welcome grant, operator
    # top-ups and commission conversions as well as purchases, so
    # ``kind = 'TOPUP'`` is not a synonym for "the customer paid".
    # ``payment_order.ledger_id`` is set only on the PENDING→PAID flip.
    assert "payment_order" in analytics._SOLD_ROWS
    assert "ledger_id" in analytics._SOLD_ROWS
    assert "TOPUP" not in analytics._SOLD_ROWS


def test_sold_subquery_excludes_nulls() -> None:
    # ``_FREE_GRANT_ROWS`` negates this subquery, and a NULL inside a
    # ``NOT IN`` list makes the whole predicate NULL for every unmatched
    # row — free grants would silently report as zero.
    assert "ledger_id IS NOT NULL" in analytics._SOLD_ROWS


def test_free_grants_are_the_grants_with_no_paid_order() -> None:
    # Defined by negating "sold" against the shared grant predicate, so
    # a future grant kind counts as a giveaway unless money came in.
    assert analytics._FREE_GRANT_ROWS == (
        f"{analytics._GRANT_ROWS} AND NOT ({analytics._SOLD_ROWS})"
    )
    # The welcome grant is a TOPUP row; keying on the kind is the bug
    # this replaced.
    assert "kind <> 'TOPUP'" not in analytics._FREE_GRANT_ROWS
