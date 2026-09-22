"""Explicit ``null`` has to survive the trip from body to column.

Every partial-edit schema on the console reads ``None`` as "leave this
alone", which is right for a description and wrong for exactly one
field: ``category_id``. Retiring a category, or correcting a skill filed
under the wrong one, are ordinary operator moves, and both are spelled
``{"category_id": null}``.

Two things used to swallow that, and this file is their regression test:
the empty-body guard, which saw a body whose every value was ``None``
and answered 422, and ``model_dump(exclude_none=True)``, which dropped
the field before the endpoint could act on it. Both now key on
``model_fields_set`` — what the caller actually sent.
"""

import pytest
from pydantic import ValidationError

from app.schemas.admin.category import CategoryUpdate
from app.schemas.admin.connector import ConnectorCatalogEntryUpdate
from app.schemas.admin.skill import SkillUpdate

#: The two edit schemas that gained ``category_id``. Same guard, same
#: endpoint-side handling, so the cases below hold for both.
_ASSIGNABLE = [SkillUpdate, ConnectorCatalogEntryUpdate]


@pytest.mark.parametrize("schema", _ASSIGNABLE)
class TestClearingACategory:
    def test_an_explicit_null_is_a_request_not_an_empty_body(self, schema):
        body = schema.model_validate({"category_id": None})

        assert "category_id" in body.model_fields_set

    def test_and_the_endpoint_can_tell_it_from_an_absent_field(self, schema):
        # This is the distinction the whole file exists for: both bodies
        # have ``category_id is None``, and only one of them is asking for
        # anything. ``model_dump(exclude_none=True)`` cannot see the
        # difference, which is why the endpoints re-add the field from
        # ``model_fields_set`` rather than from the dump.
        cleared = schema.model_validate({"category_id": None})
        untouched = schema.model_validate({"description": "unrelated edit"})

        assert cleared.category_id is None and untouched.category_id is None
        assert "category_id" not in untouched.model_fields_set

    def test_a_genuinely_empty_body_is_still_refused(self, schema):
        # The guard the ``reason`` field used to provide: without it an
        # empty PATCH is answered 200 by the endpoint's no-op path — a
        # request that did nothing, reported as success.
        with pytest.raises(ValidationError):
            schema.model_validate({})


class TestTheCategorySchemaItself:
    def test_dropping_the_translations_is_an_edit(self):
        # ``name_i18n`` is a whole-value replacement, so ``null`` clears it
        # — and has to survive the same guard to reach the endpoint.
        body = CategoryUpdate.model_validate({"name_i18n": None})

        assert "name_i18n" in body.model_fields_set
