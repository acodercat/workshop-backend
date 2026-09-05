"""An edit must not leave a field behind that its own change invalidated,
and must record faithfully what it did.

Two of the first: the locale map beside a canonical column, and the OAuth
scope on an entry that is no longer OAUTH.

The rule and its reason live in ``app.services.localized_text``. What
makes it worth a test of its own here is that breaking it is invisible
from the console: this endpoint returns the canonical column, so the
operator watches the edit take effect while the public catalog keeps
serving the old text and ``build_connector`` copies that onto every
Connector created from the entry afterwards.

The audit payload is pinned here for the same reason: it is the only copy
left of what a cleared column held, and no console screen reads it back,
so a wrong value or a colliding key is just as invisible.
"""

import json
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import connector as admin_connector
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.admin_audit_log import AdminAuditLog
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.models.staff import Staff, StaffStatus
from app.services import localized_text

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeDb:
    def __init__(self, entry: MCPServerCatalogEntry) -> None:
        self.entry = entry
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.entry

    async def scalar(self, _stmt):
        return None  # no other entry holds this URL

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _entry(**over) -> MCPServerCatalogEntry:
    entry = MCPServerCatalogEntry(
        **{
            "id": 3,
            "name": "飞书",
            "name_i18n": {"zh-CN": "飞书", "en-US": "Lark"},
            "url": "https://lark.example.com/mcp",
            "auth_type": MCPServerAuthType.OAUTH,
            "description": "旧描述",
            "description_i18n": {"zh-CN": "旧描述", "en-US": "Old blurb"},
            "published": True,
            "sort_order": 0,
            **over,
        }
    )
    entry.created_at = entry.updated_at = _NOW
    return entry


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_connector.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate_and_get_staff] = lambda: Staff(
        id=7,
        username="alice",
        password_hash="x",
        name="Alice",
        status=StaffStatus.ACTIVE,
        token_version=0,
    )
    return TestClient(app)


def _patch(db: _FakeDb, **body):
    return _client(db).patch("/connector-catalog/3", json=body)


def _audit(db: _FakeDb) -> AdminAuditLog:
    (row,) = [obj for obj in db.added if isinstance(obj, AdminAuditLog)]
    return row


def test_a_rename_reaches_every_locale() -> None:
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, name="飞书 Pro")

    # The assertion that matters is not the column but what a user reads,
    # which is what the public catalog and build_connector both go through.
    for locale in localized_text.SUPPORTED_LOCALES:
        assert localized_text.resolve(entry.name, entry.name_i18n, locale) == "飞书 Pro"


def test_a_new_description_reaches_every_locale() -> None:
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, description="新描述")

    for locale in localized_text.SUPPORTED_LOCALES:
        assert (
            localized_text.resolve_optional(entry.description, entry.description_i18n, locale)
            == "新描述"
        )


def test_the_discarded_translations_are_recorded() -> None:
    """The console can neither show nor edit these maps, so the map is
    cleared whole — and the audit payload is the only copy left of a
    hand-curated translation."""
    db = _FakeDb(_entry())

    _patch(db, name="飞书 Pro")

    payload = _audit(db).payload
    assert payload["cleared_name_i18n"] == {"zh-CN": "飞书", "en-US": "Lark"}
    assert "cleared_description_i18n" not in payload  # untouched field


def test_moving_off_oauth_clears_the_scope() -> None:
    """A scope without OAuth is meaningless, and the update schema cannot
    express "clear this" — ``None`` is "leave alone" — so a caller could
    not do it even deliberately. Left behind, it comes back the day the
    entry is switched to OAUTH again."""
    entry = _entry(oauth_scope="read write")
    db = _FakeDb(entry)

    _patch(db, auth_type="NONE")

    assert entry.oauth_scope is None
    assert _audit(db).payload["oauth_scope"] == {"from": "read write", "to": None}


def test_staying_on_oauth_keeps_the_scope() -> None:
    entry = _entry(oauth_scope="read write")
    db = _FakeDb(entry)

    _patch(db, name="飞书 Pro")

    assert entry.oauth_scope == "read write"


def test_an_edit_elsewhere_leaves_the_translations_alone() -> None:
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, sort_order=5)

    assert entry.name_i18n == {"zh-CN": "飞书", "en-US": "Lark"}
    assert entry.description_i18n == {"zh-CN": "旧描述", "en-US": "Old blurb"}
    assert "cleared_name_i18n" not in _audit(db).payload


def test_clearing_the_scope_records_the_value_it_replaced() -> None:
    """The trail must name the scope the entry *had*, not the one this
    request was carrying when it also switched auth off OAuth.

    The sibling test above only flips ``auth_type``, so ``entry.oauth_scope``
    still holds the old value by the time the clearing branch reads it and
    the bug is invisible. Change both in one request and the write loop has
    already replaced it — this pins that the ``from`` is captured first.
    ``read write`` is the only copy left once the column is nulled."""
    entry = _entry(oauth_scope="read write")
    db = _FakeDb(entry)

    _patch(db, auth_type="NONE", oauth_scope="read:new")

    assert entry.oauth_scope is None
    assert _audit(db).payload["oauth_scope"] == {"from": "read write", "to": None}


def test_a_renamed_entry_keeps_both_its_label_and_its_diff() -> None:
    """``name`` is editable here, so the label and the before → after entry
    cannot share a key: one would overwrite the other and ``payload["name"]``
    would be a string on some rows and a diff on others."""
    db = _FakeDb(_entry())

    _patch(db, name="飞书 Pro")

    payload = _audit(db).payload
    assert payload["entry_name"] == "飞书 Pro"
    assert payload["name"] == {"from": "飞书", "to": "飞书 Pro"}


def test_the_payload_records_values_not_their_reprs() -> None:
    """Raw values, as ``update_skill`` and the category endpoints write them.
    Stringified, ``published`` lands as ``"False"`` and a cleared column as
    the literal ``"None"``, which the action/created index exists to be
    queried by."""
    db = _FakeDb(_entry())

    _patch(db, published=False, sort_order=5, auth_type="NONE")

    payload = _audit(db).payload
    assert payload["published"] == {"from": True, "to": False}
    assert payload["sort_order"] == {"from": 0, "to": 5}
    # ``auth_type`` is the only non-primitive that reaches the change set,
    # and the payload is a JSONB column, so it has to survive
    # ``json.dumps``. It does only because ``MCPServerAuthType`` is a
    # ``StrEnum`` — the ``str()`` this test forbids used to make that true
    # whatever the enum's base class was, and no fake-db unit test would
    # notice it stopping.
    assert json.loads(json.dumps(payload))["auth_type"] == {
        "from": "OAUTH",
        "to": "NONE",
    }
