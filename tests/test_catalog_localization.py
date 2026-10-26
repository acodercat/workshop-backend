"""The Connector catalog in the reader's language.

Operator-authored text is the one thing next-intl cannot reach: an entry's
name and description come out of the database in whatever language they were
typed. The shape is the skills catalog's, shared as ``localized_text`` — the
column holds the canonical text, a nullable map holds the rest, and every read
falls back to the column so an untranslated entry needs no special case.
"""

import pytest

from app.api.public.mcp_server import _localized_catalog_response
from app.models.mcp_server import MCPServerAuthType
from app.models.mcp_server_catalog import MCPServerCatalogEntry
from app.services import localized_text
from app.services.connector.catalog import build_connector
from app.services.localized_text import DEFAULT_LOCALE

EN = "en-US"


def _entry(**over) -> MCPServerCatalogEntry:
    base = dict(
        id=7,
        name="天眼查",
        name_i18n={"zh-CN": "天眼查", "en-US": "Tianyancha"},
        url="https://mcp.tianyancha.com/mcp",
        auth_type=MCPServerAuthType.OAUTH,
        oauth_scope=None,
        description="查询中国企业信息",
        description_i18n={"en-US": "Search Chinese company records"},
        icon_url=None,
        published=True,
        sort_order=0,
    )
    base.update(over)
    return MCPServerCatalogEntry(**base)


class TestResolution:
    def test_a_reader_gets_their_own_language(self):
        response = _localized_catalog_response(_entry(), EN)

        assert response.name == "Tianyancha"
        assert response.description == "Search Chinese company records"

    def test_an_untranslated_entry_reads_as_stored(self):
        entry = _entry(name_i18n=None, description_i18n=None)

        response = _localized_catalog_response(entry, EN)

        assert response.name == "天眼查"
        assert response.description == "查询中国企业信息"

    def test_a_field_translated_and_one_not_are_independent(self):
        # The two maps are separate columns; a name that travelled must not
        # drag a description along, nor hold one back.
        entry = _entry(description_i18n=None)

        response = _localized_catalog_response(entry, EN)

        assert response.name == "Tianyancha"
        assert response.description == "查询中国企业信息"

    def test_a_description_nobody_wrote_stays_absent(self):
        # ``None`` and ``""`` render differently: one hides the line, the other
        # leaves an empty one.
        entry = _entry(description=None, description_i18n=None)

        assert _localized_catalog_response(entry, EN).description is None


class TestWhatIsNotExposed:
    @pytest.mark.parametrize("field", ["name_i18n", "description_i18n"])
    def test_the_maps_stay_server_side(self, field):
        # The client is handed one language, not a dictionary to choose from —
        # the same rule the operator-only columns already follow.
        from app.schemas.mcp_server import ConnectorCatalogEntryResponse

        assert field not in ConnectorCatalogEntryResponse.model_fields


class TestWhatIsCopiedOnAdd:
    def test_the_name_the_user_was_reading(self):
        connector = build_connector(_entry(), user_id=42, locale=EN)

        assert connector.name == "Tianyancha"

    def test_and_the_default_language_without_one(self):
        connector = build_connector(_entry(), user_id=42, locale=DEFAULT_LOCALE)

        assert connector.name == "天眼查"

    def test_the_copy_does_not_follow_the_catalog_afterwards(self):
        # A snapshot, not a view: the row is the user's to rename, and the
        # catalog has no way back into it. Switching the UI language later
        # leaves the Connector they added named as they first saw it.
        entry = _entry()
        connector = build_connector(entry, user_id=42, locale=EN)

        entry.name = "renamed"
        entry.name_i18n = {"en-US": "renamed too"}

        assert connector.name == "Tianyancha"


class TestTheSharedShape:
    def test_an_empty_map_is_no_map(self):
        # ``{}`` and NULL would answer ``IS NULL`` differently while meaning
        # the same thing.
        assert localized_text.clean_map({}) is None
        assert localized_text.clean_map({"en-US": "   "}) is None

    def test_unsupported_locales_are_dropped(self):
        assert localized_text.clean_map({"en-US": "a", "de-DE": "b"}) == {"en-US": "a"}

    def test_an_untranslated_locale_falls_back_to_the_default_language(self):
        assert localized_text.resolve("天眼查", {"zh-CN": "天眼查"}, "de-DE") == "天眼查"

    def test_a_request_locale_is_mapped_onto_one_we_ship(self):
        assert localized_text.normalize_locale("en-US") == "en-US"
        assert localized_text.normalize_locale("de-DE") == DEFAULT_LOCALE
        assert localized_text.normalize_locale(None) == DEFAULT_LOCALE

    def test_a_plain_string_is_one_text_for_every_locale(self):
        assert localized_text.parse("Notion", "fallback") == ("Notion", None)

    def test_a_skill_display_name_map_resolves_per_locale(self):
        default, i18n = localized_text.parse({"en-US": "Writer", "zh-CN": "写作"}, "slug")
        assert default == "写作"
        assert localized_text.resolve(default, i18n, EN) == "Writer"


class TestTheEntryRowIsUntouched:
    def test_resolving_does_not_write_back(self):
        # The response is a copy; a resolver that assigned onto the ORM row
        # would persist one reader's language for everyone at the next flush.
        entry = _entry()

        _localized_catalog_response(entry, EN)

        assert entry.name == "天眼查"
        assert entry.description == "查询中国企业信息"
