"""Operator-account invariants.

The one that matters is the last-``OWNER`` guard: ``OWNER`` is the only
role that can manage staff accounts, so a change leaving none locks
everybody out of account management, recoverable only by someone with
database credentials. It is checked **after** the change is flushed,
which is what lets a single PATCH that demotes *and* disables be caught —
the reason the service takes a whole partial update rather than one
setter per column.

A fake ``AsyncSession`` stands in for the DB; the owner-count query is
answered by whatever the fake is primed with.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import pytest

from app.errors import LastOwnerError, StaffUsernameTakenError
from app.models.staff import Staff, StaffRole, StaffStatus
from app.services.admin import staff as staff_service


class _Scalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeDb:
    """AsyncSession stand-in.

    ``scalar`` answers the username pre-check; ``scalars`` answers the
    locked owner-id query, primed with the ids that would survive the
    transaction under test.
    """

    def __init__(
        self, *, existing_username_id: int | None = None, owner_ids: list[int] = ()
    ) -> None:
        self.existing_username_id = existing_username_id
        self.owner_ids = list(owner_ids)
        self.added: list = []
        self.flushes = 0

    async def scalar(self, _stmt):
        return self.existing_username_id

    async def scalars(self, _stmt):
        return _Scalars(self.owner_ids)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


def _account(
    role: StaffRole = StaffRole.OWNER, status: StaffStatus = StaffStatus.ACTIVE
) -> Staff:
    return Staff(
        id=7,
        username="alice",
        password_hash="old-hash",
        display_name="Alice",
        role=role,
        status=status,
        token_version=0,
    )


# --- create ------------------------------------------------------------


async def test_create_hashes_the_password_and_flushes_for_an_id() -> None:
    db = _FakeDb()

    account = await staff_service.create(
        db,
        username="bob",
        display_name="Bob",
        role=StaffRole.OPERATOR,
        password="correct-horse-battery",
    )

    assert account.password_hash != "correct-horse-battery"
    assert account.role is StaffRole.OPERATOR
    # Flushed, so the caller's audit row can reference the generated id.
    assert db.flushes == 1
    assert db.added == [account]


async def test_create_refuses_a_taken_username() -> None:
    db = _FakeDb(existing_username_id=1)

    with pytest.raises(StaffUsernameTakenError):
        await staff_service.create(
            db,
            username="alice",
            display_name="Alice",
            role=StaffRole.VIEWER,
            password="correct-horse-battery",
        )

    assert db.added == []


# --- update: the last-owner invariant ----------------------------------


async def test_update_applies_only_the_fields_given() -> None:
    db = _FakeDb(owner_ids=[7])
    account = _account()

    await staff_service.update(db, account, display_name="Alice Smith")

    assert account.display_name == "Alice Smith"
    # Absent means "leave alone", never "set to None".
    assert account.role is StaffRole.OWNER
    assert account.status is StaffStatus.ACTIVE


async def test_update_allows_demoting_an_owner_when_another_remains() -> None:
    db = _FakeDb(owner_ids=[9])  # a second owner survives the change
    account = _account()

    await staff_service.update(db, account, role=StaffRole.VIEWER)

    assert account.role is StaffRole.VIEWER


async def test_update_refuses_to_demote_the_last_owner() -> None:
    db = _FakeDb(owner_ids=[])  # nobody left
    account = _account()

    with pytest.raises(LastOwnerError):
        await staff_service.update(db, account, role=StaffRole.VIEWER)


async def test_update_refuses_to_disable_the_last_owner() -> None:
    db = _FakeDb(owner_ids=[])
    account = _account()

    with pytest.raises(LastOwnerError):
        await staff_service.update(db, account, status=StaffStatus.DISABLED)


async def test_the_guard_runs_after_the_change_is_flushed() -> None:
    # The combined case: demote the last owner *and* disable them in one
    # PATCH. Checking either field alone before applying would pass this;
    # asking "does an active OWNER remain" after the flush does not.
    db = _FakeDb(owner_ids=[])
    account = _account()

    with pytest.raises(LastOwnerError):
        await staff_service.update(
            db, account, role=StaffRole.VIEWER, status=StaffStatus.DISABLED
        )

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
