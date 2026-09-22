"""The category endpoints wired up, not just their pure parts.

Covers the whole of ``_update`` / ``_create``, ``_name_fields``
included: every branch of the rename / retranslate rule is reachable
through a PATCH, so testing it twice would only mean two places to
update. What the endpoint adds over the pure function is the part that
can actually be miswired — the single commit, and the no-op path that
must not write.

Every case runs against both route groups. The module exists to write
each verb once; a case that passes for one and not the other is that
sharing having broken.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import category as admin_category
from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.exception_handlers import install_error_handlers
from app.models.connector_category import ConnectorCategory
from app.models.skill_category import SkillCategory
from app.models.staff import Staff, StaffStatus
from app.services import localized_text

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_GROUPS = [
    pytest.param("skill-categories", SkillCategory, id="skill"),
    pytest.param("connector-categories", ConnectorCategory, id="connector"),
]


class _FakeDb:
    def __init__(self, row=None) -> None:
        self.row = row
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self.row

    def add(self, obj) -> None:
        self.added.append(obj)
        self.row = obj

    async def commit(self) -> None:
        # Stands in for what ``_create`` depends on: the id and the two
        # ``server_default`` timestamps only exist once the row has
        # reached the database.
        if self.row is not None and self.row.id is None:
            self.row.id = 11
            self.row.created_at = self.row.updated_at = _NOW
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


def _category(model, **over):
    row = model(
        **{
            "id": 3,
            "name": "写作",
            "name_i18n": {"zh-CN": "写作", "en-US": "Writing"},
            "published": True,
            "sort_order": 0,
            **over,
        }
    )
    row.created_at = row.updated_at = _NOW
    return row


def _client(db: _FakeDb) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin_category.router)
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


@pytest.mark.parametrize(("path", "model"), _GROUPS)
class TestEditing:
    def test_a_rename_reaches_every_locale(self, path, model):
        # The assertion that matters is not the column but what a reader
        # gets, which is what the public tab list goes through.
        row = _category(model)
        db = _FakeDb(row)

        response = _client(db).patch(f"/{path}/3", json={"name": "创作"})

        assert response.status_code == 200
        for locale in localized_text.SUPPORTED_LOCALES:
            assert localized_text.resolve(row.name, row.name_i18n, locale) == "创作"

    def test_a_rename_with_translations_keeps_them(self, path, model):
        row = _category(model)
        db = _FakeDb(row)

        _client(db).patch(f"/{path}/3", json={"name": "创作", "name_i18n": {"en-US": "Authoring"}})

        assert localized_text.resolve(row.name, row.name_i18n, "en-US") == "Authoring"
        assert localized_text.resolve(row.name, row.name_i18n, "zh-CN") == "创作"

    def test_the_edit_lands_in_one_commit(self, path, model):
        row = _category(model)
        db = _FakeDb(row)

        _client(db).patch(f"/{path}/3", json={"published": False})

        assert row.published is False
        assert db.commits == 1

    def test_an_untranslated_rename_drops_the_map_it_invalidated(self, path, model):
        # The invalidation rule. Left behind, the old map would shadow the
        # new name in *every* locale while the console showed the edit
        # taking effect.
        row = _category(model)
        db = _FakeDb(row)

        _client(db).patch(f"/{path}/3", json={"name": "创作"})

        assert row.name_i18n is None
        assert db.commits == 1

    def test_translations_alone_leave_the_name_where_it_was(self, path, model):
        # Nothing to invalidate: the canonical column did not move.
        row = _category(model)

        _client(_FakeDb(row)).patch(f"/{path}/3", json={"name_i18n": {"en-US": "Authoring"}})

        assert row.name == "写作"
        assert row.name_i18n == {"en-US": "Authoring"}

    def test_unsupported_locales_are_dropped_on_the_way_in(self, path, model):
        # Through ``localized_text.clean_map``, the same function the
        # catalog script uses — one rule for what counts as a translation.
        row = _category(model)

        _client(_FakeDb(row)).patch(
            f"/{path}/3", json={"name_i18n": {"en-US": "Writing", "de-DE": "Schreiben"}}
        )

        assert row.name_i18n == {"en-US": "Writing"}

    def test_an_edit_that_changes_nothing_writes_nothing(self, path, model):
        # 200, because the row already reads as asked.
        db = _FakeDb(_category(model))

        response = _client(db).patch(f"/{path}/3", json={"name": "写作", "published": True})

        assert response.status_code == 200
        assert db.commits == 0

    def test_an_empty_body_is_refused(self, path, model):
        db = _FakeDb(_category(model))

        response = _client(db).patch(f"/{path}/3", json={})

        assert response.status_code == 422
        assert db.commits == 0

    def test_an_unknown_id_is_a_named_404(self, path, model):
        db = _FakeDb(None)

        response = _client(db).patch(f"/{path}/999", json={"name": "创作"})

        assert response.status_code == 404
        assert response.json()["error"]["type"] == "category_not_found"


@pytest.mark.parametrize(("path", "model"), _GROUPS)
class TestCreating:
    def test_a_new_category_is_published_and_committed(self, path, model):
        db = _FakeDb()

        response = _client(db).post(f"/{path}", json={"name": "研究"})

        assert response.status_code == 201
        assert response.json()["published"] is True
        assert response.json()["id"] == 11
        assert db.commits == 1

    def test_unusable_translations_are_stored_as_no_map(self, path, model):
        # ``{}`` and NULL would answer ``IS NULL`` differently while
        # meaning the same thing.
        db = _FakeDb()

        response = _client(db).post(f"/{path}", json={"name": "研究", "name_i18n": {"de-DE": "x"}})

        assert response.status_code == 201
        assert response.json()["name_i18n"] is None
