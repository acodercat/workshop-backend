"""``apply_topup`` — every grant records where it came from.

Every welcome grant used to read ``source: admin`` on its ledger row.
"""

from unittest.mock import AsyncMock

from app.services.credits import topup


async def test_the_source_reaches_the_ledger_row(monkeypatch) -> None:
    insert = AsyncMock()
    monkeypatch.setattr(topup, "insert_ledger_and_bump_balance", insert)

    await topup.apply_topup(
        AsyncMock(),
        user_id=7,
        amount_micro=10,
        idempotency_key="welcome-7",
        reason="r",
        source="welcome_grant",
    )

    assert insert.await_args.kwargs["reference"]["source"] == "welcome_grant"
