"""``insert_ledger_and_bump_balance`` — the single ledger write primitive.

Focused on the ``reservation_id`` promotion: it must land on the typed
column, never be smuggled back into the ``reference`` JSONB. A fake
``AsyncSession`` captures the ORM row without touching a DB (the balance
UPDATE result is ignored; the lock SELECT returns a stub balance).

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

from app.models.credit_ledger import CreditLedgerKind
from app.services.credits._internal import insert_ledger_and_bump_balance


class _Result:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one_or_none(self) -> int:
        return self._value


class _FakeDb:
    """Minimal AsyncSession stand-in: ``execute`` serves both the
    ``lock_user_balance`` SELECT (via ``scalar_one_or_none``) and the
    balance UPDATE (result ignored); ``add`` captures the ledger row."""

    def __init__(self, balance: int) -> None:
        self.balance = balance
        self.added: list = []

    async def execute(self, _stmt):
        return _Result(self.balance)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


async def test_reservation_id_lands_on_the_column_not_jsonb() -> None:
    db = _FakeDb(balance=1_000)

    entry = await insert_ledger_and_bump_balance(
        db,
        user_id=1,
        amount=-50,
        kind=CreditLedgerKind.RESERVE,
        reservation_id=6069,
    )

    assert entry is db.added[0]
    assert entry.reservation_id == 6069
    # The whole point of the promotion: it's a first-class column, never
    # a JSONB key.
    assert "reservation_id" not in (entry.reference or {})


async def test_reservation_id_defaults_to_none_for_non_turn_rows() -> None:
    db = _FakeDb(balance=1_000)

    entry = await insert_ledger_and_bump_balance(
        db,
        user_id=1,
        amount=100,
        kind=CreditLedgerKind.TOPUP,
        idempotency_key="topup-1",
    )

    assert entry.reservation_id is None
