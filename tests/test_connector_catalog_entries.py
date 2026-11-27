"""The rules a catalog entry keeps whoever writes it —
:func:`app.services.connector.catalog.create_entry` and ``update_entry``.

The database is a fake that answers the two lookups the rules make: is the
URL taken, does the category exist.
"""

import pytest

from app.errors import CategoryNotFoundError, ConnectorCatalogUrlTakenError
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.services.connector import catalog


class _Db:
    def __init__(self, *, url_taken: bool = False, category_exists: bool = True) -> None:
        self._url_taken = url_taken
        self._category_exists = category_exists
        self.added: list[object] = []
        self.flushed = False

    async def scalar(self, statement: object) -> int | None:
        return 1 if self._url_taken else None

    async def get(self, model: object, key: int) -> object | None:
        return object() if self._category_exists else None

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True


def _entry(**overrides: object) -> MCPServerCatalogEntry:
    values: dict[str, object] = {
        "name": "Notes",
        "name_i18n": {"zh": "笔记"},
        "description": "Search notes.",
        "description_i18n": {"zh": "搜索笔记。"},
        "url": "https://notes.example.com/mcp",
        "auth_type": MCPServerAuthType.OAUTH,
        "oauth_scope": "read",
    }
    return MCPServerCatalogEntry(**(values | overrides))


class TestCreateEntry:
    async def test_it_is_added_and_flushed_but_not_committed(self) -> None:
        db = _Db()

        entry = await catalog.create_entry(
            db, name="Notes", url="https://notes.example.com/mcp", auth_type=MCPServerAuthType.NONE
        )

        assert db.added == [entry]
        assert db.flushed
        assert not hasattr(db, "commit")

    async def test_a_url_the_catalog_offers_is_refused_before_anything_is_written(self) -> None:
        db = _Db(url_taken=True)

        with pytest.raises(ConnectorCatalogUrlTakenError):
            await catalog.create_entry(
                db, name="Notes", url="https://taken.example.com", auth_type=MCPServerAuthType.NONE
            )

        assert db.added == []

    async def test_a_category_the_taxonomy_lacks_is_refused(self) -> None:
        db = _Db(category_exists=False)

        with pytest.raises(CategoryNotFoundError):
            await catalog.create_entry(
                db,
                name="Notes",
                url="https://notes.example.com/mcp",
                auth_type=MCPServerAuthType.NONE,
                category_id=9,
            )

        assert db.added == []


class TestUpdateEntry:
    async def test_rewording_a_column_clears_its_locale_map_and_returns_it(self) -> None:
        entry = _entry()

        cleared = await catalog.update_entry(_Db(), entry, {"name": "Notebook"})

        assert entry.name == "Notebook"
        assert entry.name_i18n is None
        assert cleared == {"name_i18n": {"zh": "笔记"}}
        # The column the edit did not reword keeps its translations.
        assert entry.description_i18n == {"zh": "搜索笔记。"}

    async def test_a_column_sent_unchanged_keeps_its_locale_map(self) -> None:
        entry = _entry()

        cleared = await catalog.update_entry(_Db(), entry, {"name": "Notes", "sort_order": 4})

        assert cleared == {}
        assert entry.name_i18n == {"zh": "笔记"}

    async def test_leaving_oauth_clears_the_scope_and_returns_what_it_was(self) -> None:
        entry = _entry()

        cleared = await catalog.update_entry(_Db(), entry, {"auth_type": MCPServerAuthType.NONE})

        assert entry.oauth_scope is None
        assert cleared == {"oauth_scope": "read"}

    async def test_a_url_already_offered_is_refused_before_the_entry_changes(self) -> None:
        entry = _entry()

        with pytest.raises(ConnectorCatalogUrlTakenError):
            await catalog.update_entry(
                _Db(url_taken=True), entry, {"url": "https://taken.example.com", "name": "X"}
            )

        assert entry.name == "Notes"

    async def test_the_entrys_own_url_is_not_a_collision(self) -> None:
        entry = _entry()

        await catalog.update_entry(_Db(url_taken=True), entry, {"url": entry.url})
