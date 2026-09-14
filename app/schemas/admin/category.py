"""Request / response models for the console's two category taxonomies.

One set of models for both ``skill_category`` and ``connector_category``:
the tables are separate so a query cannot reach across them, but the
shapes are identical and the console edits them on one screen. Splitting
these into four near-identical classes would buy nothing the foreign key
does not already guarantee.

Two deliberate departures from :mod:`app.schemas.admin.connector`, whose
create/update pair these otherwise follow: the locale map is **in** the
response, and a new row is created **published**. Both follow from the
same fact — a category is *only* a name, where a catalog entry has a
dozen fields and a name among them — and each class below says how.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CategoryResponse(BaseModel):
    """One category, published or not.

    ``name_i18n`` is present here, unlike on
    :class:`~app.schemas.admin.connector.ConnectorCatalogEntryResponse`,
    which hides its locale maps. That surface hides them because its
    console cannot edit them, so showing a map an operator could not
    change would only raise a question. This console *does* edit them
    (see :class:`CategoryUpdate`), and hiding what the operator just
    wrote would leave them editing blind.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    name_i18n: dict[str, str] | None
    published: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class CategoryCreate(BaseModel):
    """A new category.

    ``published`` defaults to ``True``, where a catalog entry defaults to
    ``False``. An entry is staged unpublished so an operator can check
    its URL resolves and its icon renders; a category is a name, with
    nothing to check. The default also matches the column's
    ``server_default``, so a row created here and a row created by a
    migration read the same.
    """

    name: str = Field(min_length=1, max_length=64)
    name_i18n: dict[str, str] | None = None
    published: bool = True
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    """A partial edit. Absent fields are left alone.

    ``name_i18n`` is the one field where an explicit ``null`` means
    "clear": it is a whole-value replacement, and dropping the
    translations is a thing an operator does. The endpoint reads
    ``model_fields_set`` rather than the dumped values to tell an
    explicit ``null`` from an absent field.
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    name_i18n: dict[str, str] | None = None
    published: bool | None = None
    sort_order: int | None = None

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "CategoryUpdate":
        """Refuse a request that asks for nothing.

        Keyed on ``model_fields_set`` — what the caller actually sent —
        rather than on the non-``None`` values, so ``{"name_i18n": null}``
        reads as the change it is instead of as an empty body.
        """
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to change.")
        return self
