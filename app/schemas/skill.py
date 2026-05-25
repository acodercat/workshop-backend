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
    name: str
    description: str
    bundle_size: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SkillCreate(BaseModel):
    """Body of ``POST /api/skills`` — register a previously-uploaded
    bundle. The backend pulls the zip from OSS, parses SKILL.md to
    extract the picker name + description, and persists the row.
    """
    bundle_key: str = Field(
        ..., description="OSS key returned by POST /api/skills/upload-url",
    )


class SkillUpdate(BaseModel):
    """Body of ``PATCH /api/skills/{id}`` — all fields optional; the
    bundle itself is immutable (re-upload to change it)."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool | None = None
