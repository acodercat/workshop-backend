"""The console's user slice — the status flip and the money path.

Exercised through a real app (same pattern as ``test_admin_signin``):
``get_db`` and ``authenticate_and_get_staff`` are dependency-overridden,
and the ledger funnel is patched at the ``credits`` package the endpoint
calls through — the funnel has its own tests; what's under test here is
the endpoint's contract with it (unit conversion, key namespacing,
operator attribution) and the audit rows.
"""

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import user as admin_user
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.admin_audit_log import AdminAuditLog
from app.models.staff import Staff, StaffStatus
from app.models.user import User, UserStatus
from app.services.credits.adjust import AdjustmentAlreadyApplied


class _FakeDb:
    """Stand-in AsyncSession: ``get`` serves the PK lookup, ``add`` and
    ``commit`` record what the audit unit of work stages."""

    def __init__(self, user: User | None) -> None:
        self.user = user
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.user

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        pass


def _staff() -> Staff:
    return Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )


def _user(status: UserStatus = UserStatus.ACTIVE) -> User:
    user = User(
        id=42,
        username="axon_42",
        name="Bob",
        password_hash="x",
        email="bob@example.com",
        phone=None,
        status=status,
        credit_balance=50_000_000,  # 5 credits
    )
    user.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    user.last_login_at = None
    return user


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_user.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate_and_get_staff] = _staff
    return TestClient(app)


def _audit_rows(db: _FakeDb) -> list[AdminAuditLog]:
    return [obj for obj in db.added if isinstance(obj, AdminAuditLog)]


# --- PATCH /users/{id} --------------------------------------------------


def test_status_change_applies_and_audits() -> None:
    db = _FakeDb(_user())

    response = _client(db).patch("/users/42", json={"status": "DISABLED"})

    assert response.status_code == 200
    assert response.json()["status"] == "DISABLED"
    (row,) = _audit_rows(db)
    assert row.action == "user.update"
    assert (row.target_type, row.target_id) == ("user", "42")
    assert row.payload == {"status": {"from": "ACTIVE", "to": "DISABLED"}}
    assert db.commits == 1


def test_a_noop_status_change_writes_no_audit_row() -> None:
    db = _FakeDb(_user(UserStatus.ACTIVE))

    response = _client(db).patch("/users/42", json={"status": "ACTIVE"})

    assert response.status_code == 200
    assert _audit_rows(db) == []
    assert db.commits == 0


def test_unknown_user_is_404() -> None:
    response = _client(_FakeDb(user=None)).patch("/users/999", json={"status": "DISABLED"})

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "user_not_found"


# --- POST /users/{id}/credits/adjustments -------------------------------


class _Ledger:
    id = 1234


def test_adjustment_converts_namespaces_and_attributes(monkeypatch) -> None:
    db = _FakeDb(_user())
    calls = {}

    async def fake_apply_adjust(_db, **kwargs):
        calls.update(kwargs)
        return _Ledger()

    async def fake_get_balance(_db, _user_id):
        return 170_000_000  # 17 credits after the grant

    monkeypatch.setattr(admin_user.credits, "apply_adjust", fake_apply_adjust)
    monkeypatch.setattr(admin_user.credits, "get_balance", fake_get_balance)

    response = _client(db).post(
        "/users/42/credits/adjustments",
        json={
            "amount_credits": 12,
            "idempotency_key": "3f2e1d0c-form",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "ledger_id": 1234,
        "amount_credits": 12,
        "balance_credits": 17,
    }
    # The endpoint's whole job: whole credits → µ-credits, the console
    # namespace on the key, and the operator on the ledger row.
    assert calls["amount_micro"] == 120_000_000
    assert calls["idempotency_key"] == "admin:3f2e1d0c-form"
    assert calls["admin_id"] == 7
    # The console no longer asks for a justification, but ``apply_adjust``
    # still takes one (the CLI passes a real ``--reason``), so the endpoint
    # names itself rather than forwarding an empty string.
    assert calls["reason"] == "admin console adjustment"
    (row,) = _audit_rows(db)
    assert row.action == "credit.adjust"
    assert row.payload == {"amount_credits": 12, "ledger_id": 1234}


def test_duplicate_idempotency_key_is_409_with_no_audit_row(monkeypatch) -> None:
    db = _FakeDb(_user())

    async def fake_apply_adjust(_db, **_kwargs):
        raise AdjustmentAlreadyApplied("dup")

    monkeypatch.setattr(admin_user.credits, "apply_adjust", fake_apply_adjust)

    response = _client(db).post(
        "/users/42/credits/adjustments",
        json={"amount_credits": 12, "idempotency_key": "same-key-1"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "adjustment_already_applied"
    # The adjustment never applied, so the trail must not claim it did.
    assert _audit_rows(db) == []
    assert db.commits == 0


def test_zero_amount_is_rejected_at_validation() -> None:
    response = _client(_FakeDb(_user())).post(
        "/users/42/credits/adjustments",
        json={"amount_credits": 0, "idempotency_key": "some-key-1"},
    )

    assert response.status_code == 422
