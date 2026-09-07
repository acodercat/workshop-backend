"""Every console mutation commits itself.

This deployment keeps no operator trail: the ``audit()`` funnel that used
to own the commit was removed with ``admin_audit_log`` (revision
``c8f2a5e1d94b``). The funnel had a side benefit worth more than the
trail on some days — an endpoint that forgot to audit also failed to
save, so a missing commit was impossible rather than merely unlikely.
Nothing structural replaces it, so these tests do: one per mutation,
asserting the endpoint awaited ``commit`` after the service call.

The service layer is patched out. What is under test is not what the
mutation does — that has its own tests — but whether the endpoint
persists it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.api.admin import me as admin_me, staff as admin_staff
from app.models.staff import Staff, StaffStatus
from app.schemas.admin.staff import (
    StaffCreate,
    StaffPasswordChange,
    StaffPasswordReset,
    StaffUpdate,
)
from app.services.admin import AdminContext


class _FakeDb:
    """Records the ordering the tests care about: did ``commit`` happen,
    and did it happen *after* the service call."""

    def __init__(self, account: Staff | None = None) -> None:
        self._account = account
        self.commits = 0

    async def get(self, _model, _pk):
        return self._account

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class _Env:
    ctx: AdminContext
    db: _FakeDb
    account: Staff


def _account(staff_id: int = 1) -> Staff:
    return Staff(
        id=staff_id,
        username="op",
        name="Operator",
        password_hash="hashed",
        status=StaffStatus.ACTIVE,
        last_login_at=None,
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
    )


@pytest.fixture
def env() -> _Env:
    account = _account()
    db = _FakeDb(account)
    return _Env(ctx=AdminContext(db=db, staff=account), db=db, account=account)


async def test_create_staff_commits(env, monkeypatch) -> None:
    monkeypatch.setattr(
        admin_staff.staff_service, "create", AsyncMock(return_value=env.account)
    )

    await admin_staff.create_staff(
        env.ctx, StaffCreate(username="new", name="New", password="a" * 12)
    )

    assert env.db.commits == 1


async def test_update_staff_commits(env, monkeypatch) -> None:
    monkeypatch.setattr(admin_staff.staff_service, "update", AsyncMock())

    await admin_staff.update_staff(env.ctx, env.account.id, StaffUpdate(name="Renamed"))

    assert env.db.commits == 1


async def test_reset_staff_password_commits(env, monkeypatch) -> None:
    monkeypatch.setattr(admin_staff.staff_service, "set_password", AsyncMock())

    await admin_staff.reset_staff_password(
        env.ctx, env.account.id, StaffPasswordReset(password="b" * 12)
    )

    assert env.db.commits == 1


async def test_change_my_password_commits(env, monkeypatch) -> None:
    monkeypatch.setattr(admin_me, "verify_password", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_me.staff_service, "set_password", AsyncMock())

    await admin_me.change_my_password(
        env.ctx, StaffPasswordChange(current_password="c" * 12, new_password="d" * 12)
    )

    assert env.db.commits == 1


async def test_a_refused_password_change_commits_nothing(env, monkeypatch) -> None:
    """The sudo gate runs before the mutation, so a wrong current password
    must leave the session untouched — not merely unchanged."""
    monkeypatch.setattr(admin_me, "verify_password", AsyncMock(return_value=False))
    set_password = AsyncMock()
    monkeypatch.setattr(admin_me.staff_service, "set_password", set_password)

    with pytest.raises(admin_me.IncorrectPasswordError):
        await admin_me.change_my_password(
            env.ctx,
            StaffPasswordChange(current_password="wrong-one", new_password="d" * 12),
        )

    set_password.assert_not_awaited()
    assert env.db.commits == 0
