"""An edit must not leave a field behind that its own change invalidated.

Two of those: the locale map beside a canonical column, and the OAuth
scope on an entry that is no longer OAUTH.

The rule and its reason live in ``app.services.localized_text``. What
makes it worth a test of its own here is that breaking it is invisible
from the console: this endpoint returns the canonical column, so the
operator watches the edit take effect while the public catalog keeps
serving the old text and ``build_connector`` copies that onto every
Connector created from the entry afterwards.
"""

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import connector as admin_connector
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
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


def test_a_rename_reaches_every_locale() -> None:
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, name="飞书 Pro")

    # The assertion that matters is not the column but what a user reads,
    # which is what the public catalog and build_connector both go through.
    for locale in localized_text.SUPPORTED_LOCALES:
        assert localized_text.resolve(entry.name, entry.name_i18n, locale) == "飞书 Pro"
    assert db.commits == 1


def test_a_new_description_reaches_every_locale() -> None:
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, description="新描述")

    for locale in localized_text.SUPPORTED_LOCALES:
        assert (
            localized_text.resolve_optional(entry.description, entry.description_i18n, locale)
            == "新描述"
        )


def test_a_rename_clears_only_the_map_it_invalidated() -> None:
    """The console can neither show nor edit these maps, so the one beside
    the changed column is cleared whole — and only that one."""
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, name="飞书 Pro")

    assert entry.name_i18n is None
    assert entry.description_i18n == {"zh-CN": "旧描述", "en-US": "Old blurb"}


def test_moving_off_oauth_clears_the_scope() -> None:
    """A scope without OAuth is meaningless, and the update schema cannot
    express "clear this" — ``None`` is "leave alone" — so a caller could
    not do it even deliberately. Left behind, it comes back the day the
    entry is switched to OAUTH again."""
    entry = _entry(oauth_scope="read write")
    db = _FakeDb(entry)

    _patch(db, auth_type="NONE")

    assert entry.auth_type is MCPServerAuthType.NONE
    assert entry.oauth_scope is None


def test_moving_off_oauth_clears_a_scope_sent_in_the_same_request() -> None:
    entry = _entry(oauth_scope="read write")
    db = _FakeDb(entry)

    _patch(db, auth_type="NONE", oauth_scope="read:new")

    assert entry.oauth_scope is None


def test_moving_off_oauth_ends_the_preregistered_app() -> None:
    """The app is a client secret; kept on an entry that cannot use it, it
    is a credential nobody is watching."""
    entry = _entry(oauth_client_id="client-1", oauth_client_secret_encrypted="sealed")
    db = _FakeDb(entry)

    response = _patch(db, auth_type="NONE")

    assert entry.oauth_client_id is None
    assert entry.oauth_client_secret_encrypted is None
    assert db.commits == 1
    assert response.json()["has_preregistered_client"] is False


def test_staying_on_oauth_keeps_the_scope() -> None:
    entry = _entry(oauth_scope="read write")
    db = _FakeDb(entry)

    _patch(db, name="飞书 Pro")

    assert entry.oauth_scope == "read write"


def test_an_edit_elsewhere_leaves_the_translations_alone() -> None:
    entry = _entry()
    db = _FakeDb(entry)

    _patch(db, sort_order=5)

    assert entry.sort_order == 5
    assert entry.name_i18n == {"zh-CN": "飞书", "en-US": "Lark"}
    assert entry.description_i18n == {"zh-CN": "旧描述", "en-US": "Old blurb"}


def test_an_edit_that_changes_nothing_writes_nothing() -> None:
    db = _FakeDb(_entry())

    response = _patch(db, name="飞书", published=True)

    assert response.status_code == 200
    assert db.commits == 0
