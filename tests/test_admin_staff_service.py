"""Operator-account invariants.

The one that matters is the lockout guard: a status change must never
leave the console with zero ``ACTIVE`` accounts, because recovery then
requires database credentials and ``scripts/manage_staff.py``. It is
checked **after** the change is flushed, so the question asked is about
the state the transaction ends in.

A fake ``AsyncSession`` stands in for the DB; the active-account query is
answered by whatever the fake is primed with.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import pytest

from app.errors import LastActiveStaffError, StaffUsernameTakenError
from app.models.staff import Staff, StaffStatus
from app.services.admin import staff as staff_service


class _Scalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeDb:
    """AsyncSession stand-in.

    ``scalar`` answers the username pre-check; ``scalars`` answers the
    locked active-account query, primed with the ids that would survive
    the transaction under test.
    """

    def __init__(
        self, *, existing_username_id: int | None = None, active_ids: list[int] = ()
    ) -> None:
        self.existing_username_id = existing_username_id
        self.active_ids = list(active_ids)
        self.added: list = []
        self.flushes = 0

    async def scalar(self, _stmt):
        return self.existing_username_id

    async def scalars(self, _stmt):
        return _Scalars(self.active_ids)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


def _account(status: StaffStatus = StaffStatus.ACTIVE) -> Staff:
    return Staff(
        id=7,
        username="alice",
        password_hash="old-hash",
        name="Alice",
        status=status,
        token_version=0,
    )


# --- create ------------------------------------------------------------


async def test_create_hashes_the_password_and_flushes_for_an_id() -> None:
    db = _FakeDb()

    account = await staff_service.create(
        db, username="bob", name="Bob", password="correct-horse-battery"
    )

    assert account.password_hash != "correct-horse-battery"
    # Flushed, so the caller's audit row can reference the generated id.
    assert db.flushes == 1
    assert db.added == [account]


async def test_create_refuses_a_taken_username() -> None:
    db = _FakeDb(existing_username_id=1)

    with pytest.raises(StaffUsernameTakenError):
        await staff_service.create(
            db, username="alice", name="Alice", password="correct-horse-battery"
        )

    assert db.added == []


# --- update: the lockout guard ------------------------------------------


async def test_update_applies_only_the_fields_given() -> None:
    db = _FakeDb(active_ids=[7])
    account = _account()

    await staff_service.update(db, account, name="Alice Smith")

    assert account.name == "Alice Smith"
    # Absent means "leave alone", never "set to None".
    assert account.status is StaffStatus.ACTIVE


async def test_a_name_change_skips_the_guard() -> None:
    # A rename can't lock anyone out, so it must not touch (or lock) the
    # other rows at all.
    db = _FakeDb(active_ids=[])  # guard would raise if consulted
    account = _account()

    await staff_service.update(db, account, name="Alice Smith")

    assert account.name == "Alice Smith"


async def test_disabling_is_allowed_while_another_active_account_remains() -> None:
    db = _FakeDb(active_ids=[9])  # a second active account survives
    account = _account()

    await staff_service.update(db, account, status=StaffStatus.DISABLED)

    assert account.status is StaffStatus.DISABLED


async def test_disabling_the_last_active_account_is_refused() -> None:
    db = _FakeDb(active_ids=[])  # nobody left
    account = _account()

    with pytest.raises(LastActiveStaffError):
        await staff_service.update(db, account, status=StaffStatus.DISABLED)


async def test_the_guard_runs_after_the_change_is_flushed() -> None:
    # The guard asks "does an ACTIVE account remain in the state this
    # transaction commits", which requires the change to be flushed
    # first — not a pre-flight check against the old state.
    db = _FakeDb(active_ids=[])
    account = _account()

    with pytest.raises(LastActiveStaffError):
        await staff_service.update(db, account, status=StaffStatus.DISABLED)

    assert db.flushes == 1


# --- passwords ---------------------------------------------------------


async def test_set_password_rehashes_and_signs_the_account_out() -> None:
    db = _FakeDb()
    account = _account()

    await staff_service.set_password(db, account, "correct-horse-battery")

    assert account.password_hash not in ("old-hash", "correct-horse-battery")
    # Revocation is not optional: the reason to rotate a console password
    # is a leak or a departure, and leaving old tokens live defeats both.
    assert account.token_version == 1


def test_the_password_floor_is_above_the_customer_facing_one() -> None:
    # Console accounts guard the highest-privilege surface in the system.
    assert staff_service.MIN_PASSWORD_CHARS >= 12
