"""Schemas for the /api/skills surface (per-user uploaded skill bundles)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SkillResponse(BaseModel):
    """One row of the user's skill list rendered for the picker.

    ``bundle_key`` (the internal OSS path) is deliberately omitted —
    the frontend never PUTs or fetches by it, and exposing storage-
    layer keys is a needless implementation leak.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    origin: str
    name: str
    display_name: str
    description: str
    bundle_size: int
    enabled: bool
    # The tab this skill browses under; ``None`` is uncategorised, and so
    # is an id the client cannot resolve. The id alone, never the name —
    # ``list_skill_categories`` explains why.
    category_id: int | None
    created_at: datetime
    updated_at: datetime


class SkillUpdate(BaseModel):
    """Body of ``PATCH /api/skills/{id}`` — mutable picker metadata only.

    ``name`` is immutable (derived from the bundle's SKILL.md frontmatter;
    change it by re-uploading). ``display_name``, ``description``, and
    ``enabled`` are editable without touching the bundle.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=1024)
    enabled: bool | None = None


class SkillCategoryResponse(BaseModel):
    """One tab on the skills page, already localized.

    Its own class rather than one shared with the Connector catalog's:
    the two taxonomies are separate tables, and a generated client
    should not be able to hand an id from one to an endpoint expecting
    the other.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: Resolved for the request locale. ``name_i18n`` never leaves the
    #: backend — the client renders this string as-is.
    name: str
