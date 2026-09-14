"""A tab in the reader's language.

Same shape as the two catalogs it groups — canonical column, nullable
map beside it, every read falling back to the column — so an
untranslated category needs no special case. What is different is how
little there is: a category *is* its name, so the fallback is the whole
of what a client renders when a translation is missing.
"""

import pytest

from app.api.public.mcp_server import _localized_connector_category_response
from app.api.public.skill import _localized_category_response
from app.models.connector_category import ConnectorCategory
from app.models.skill_category import SkillCategory

EN = "en-US"

#: Both helpers, paired with the model each reads, so every case below
#: is asserted once per taxonomy. The two are separate tables and
#: separate response classes precisely so they cannot be confused —
#: which is also why neither may quietly diverge from the other here.
_TAXONOMIES = [
    pytest.param(_localized_category_response, SkillCategory, id="skill"),
    pytest.param(_localized_connector_category_response, ConnectorCategory, id="connector"),
]


def _category(model, **over) -> SkillCategory | ConnectorCategory:
    base = dict(
        id=3,
        name="写作",
        name_i18n={"zh-CN": "写作", "en-US": "Writing"},
        published=True,
        sort_order=0,
    )
    base.update(over)
    return model(**base)


@pytest.mark.parametrize(("localize", "model"), _TAXONOMIES)
class TestResolution:
    def test_a_reader_gets_their_own_language(self, localize, model):
        assert localize(_category(model), EN).name == "Writing"

    def test_an_untranslated_category_reads_as_stored(self, localize, model):
        # The column is the fallback, so a category nobody translated is
        # still a usable tab rather than a blank one.
        assert localize(_category(model, name_i18n=None), EN).name == "写作"

    def test_the_map_stays_server_side(self, localize, model):
        # One language, not a dictionary to choose from — the same rule the
        # two catalogs already follow.
        response = localize(_category(model), EN)

        assert "name_i18n" not in type(response).model_fields

    def test_resolving_does_not_write_back(self, localize, model):
        # The response is a copy. A resolver that assigned onto the ORM row
        # would persist one reader's language for everyone at the next flush.
        category = _category(model)

        localize(category, EN)

        assert category.name == "写作"
