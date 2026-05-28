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
