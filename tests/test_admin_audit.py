"""The audit unit of work.

The console's central invariant is that a mutation and its audit row
commit together, or neither does. These tests pin the three ways that can
break: committing without a row, writing a row for work that failed, and
letting a credential reach the trail.

A fake ``AsyncSession`` records ``add``/``commit`` order without touching
a database.

asyncio_mode = "auto" (see pyproject.toml), so no ``@pytest.mark.asyncio``.
"""

import pytest

from app.models.admin_audit_log import AdminAuditLog
from app.models.staff import Staff, StaffRole, StaffStatus
from app.services.admin import AdminContext, AuditAction, audit


class _FakeDb:
    """Minimal AsyncSession stand-in recording the write sequence."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        pass


def _context(db: _FakeDb) -> AdminContext:
    return AdminContext(
        db=db,
        staff=Staff(
            id=7,
            username="alice",
            password_hash="x",
            display_name="Alice",
            role=StaffRole.OWNER,
            status=StaffStatus.ACTIVE,
            token_version=0,
        ),
        request_id="req-123",
        ip_address="203.0.113.5",
    )


def _entry(db: _FakeDb) -> AdminAuditLog:
    (row,) = [obj for obj in db.added if isinstance(obj, AdminAuditLog)]
    return row


# --- atomicity ---------------------------------------------------------


async def test_commits_once_after_staging_the_row() -> None:
    db = _FakeDb()

    async with audit(
        _context(db),
        action=AuditAction.STAFF_UPDATE,
        target_type="staff",
        target_id=42,
    ):
        db.add("the mutation")

    # One commit, and the audit row was staged before it — the mutation
    # and the record land in the same transaction.
    assert db.commits == 1
    assert isinstance(db.added[-1], AdminAuditLog)


async def test_a_failing_block_writes_nothing_and_does_not_commit() -> None:
    db = _FakeDb()

    with pytest.raises(RuntimeError):
        async with audit(_context(db), action=AuditAction.STAFF_CREATE):
            db.add("a mutation that is about to fail")
            raise RuntimeError("boom")

    # No commit means the surrounding session rolls back, taking the
    # half-done mutation with it. An action that failed leaves no trace
    # claiming it happened.
    assert db.commits == 0
    assert not any(isinstance(obj, AdminAuditLog) for obj in db.added)


# --- what lands on the row ---------------------------------------------


async def test_stamps_operator_action_and_request_metadata() -> None:
    db = _FakeDb()

    async with audit(
        _context(db),
        action=AuditAction.STAFF_PASSWORD_RESET,
        target_type="staff",
        target_id=42,
        reason="offboarding",
    ):
        pass

    row = _entry(db)
    assert row.staff_id == 7
    assert row.action == "staff.password_reset"
    assert row.target_type == "staff"
    assert row.reason == "offboarding"
    assert row.request_id == "req-123"
    assert row.ip_address == "203.0.113.5"


async def test_target_id_is_stringified_for_the_shared_column() -> None:
    # Targets are keyed variously by int and UUID across tables; one
    # VARCHAR column holds them all, and the coercion happens here rather
    # than at every call site.
    db = _FakeDb()

    async with audit(
        _context(db),
        action=AuditAction.STAFF_UPDATE,
        target_type="staff",
        target_id=42,
    ):
        pass

    assert _entry(db).target_id == "42"


async def test_the_block_can_set_a_target_it_only_learns_inside() -> None:
    # A create has no id until the flush, so the draft is completed in
    # the block rather than passed in.
    db = _FakeDb()

    async with audit(_context(db), action=AuditAction.STAFF_CREATE) as draft:
        draft.target_type = "staff"
        draft.target_id = 99
        draft.payload = {"username": "bob"}

    row = _entry(db)
    assert row.target_id == "99"
    assert row.payload == {"username": "bob"}


async def test_an_action_with_no_target_records_none() -> None:
    db = _FakeDb()

    async with audit(_context(db), action=AuditAction.STAFF_LOGIN):
        pass

    row = _entry(db)
    assert row.target_type is None
    assert row.target_id is None
    assert row.payload == {}


# --- redaction ---------------------------------------------------------


async def test_credential_shaped_keys_never_reach_the_trail() -> None:
    db = _FakeDb()

    async with audit(_context(db), action=AuditAction.STAFF_UPDATE) as draft:
        draft.payload = {
            "password": "hunter2",
            "new_Password": "hunter3",
            "api_key": "sk-live-1",
            "refresh_token": "rt-1",
            "authorization": "Bearer abc",
            "client_secret": "s3cret",
            "username": "bob",
        }

    payload = _entry(db).payload
    assert payload["username"] == "bob"
    redacted = {key for key, value in payload.items() if value == "[redacted]"}
    assert redacted == set(payload) - {"username"}
