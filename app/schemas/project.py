"""Schemas for the /api/projects surface."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.text import printable

# What the instructions dialog lets the owner type. Every session in the
# project carries these in its system prompt, so this bounds a cost paid on
# every turn, not just storage.
_INSTRUCTIONS_MAX_LENGTH = 8000


def _checked_name(value: str | None) -> str | None:
    """Trim a project name, and refuse one that shows nothing.

    ``None`` passes: on ``PATCH`` it means "leave the name alone".

    A name made only of invisible characters — zero-width spaces, say —
    passes ``min_length`` and would list as a blank row in the sidebar.
    U+0000 is not representable in a ``varchar`` column, so it is refused
    here rather than failing the INSERT as an opaque 500.
    """
    if value is None:
        return None
    if "\x00" in value:
        raise ValueError("name must not contain a null character")
    name = value.strip()
    if not printable(name).strip():
        raise ValueError("name must contain visible text")
    return name


def _checked_instructions(value: str | None) -> str | None:
    """Refuse instructions that cannot be stored. Empty is allowed: it is
    how the owner clears them."""
    if value is not None and "\x00" in value:
        raise ValueError("instructions must not contain a null character")
    return value


class ProjectCreate(BaseModel):
    """Body of ``POST /api/projects``."""

    name: str = Field(..., min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=_INSTRUCTIONS_MAX_LENGTH)

    _check_name = field_validator("name")(_checked_name)
    _check_instructions = field_validator("instructions")(_checked_instructions)


class ProjectUpdate(BaseModel):
    """Body of ``PATCH /api/projects/{id}``. ``None`` leaves a field alone."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    instructions: str | None = Field(default=None, max_length=_INSTRUCTIONS_MAX_LENGTH)

    _check_name = field_validator("name")(_checked_name)
    _check_instructions = field_validator("instructions")(_checked_instructions)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    instructions: str
    created_at: datetime
    updated_at: datetime
