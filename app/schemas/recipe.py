"""Schemas for the /api/recipes surface (user-selectable prompt scaffolds)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RecipeResponse(BaseModel):
    """One recipe rendered for the picker / management list.

    ``instructions`` is included so the management UI can show the
    scaffold's body (and let the owner edit it); ``name`` is the stable
    internal identifier, surfaced read-only.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    origin: str
    name: str
    display_name: str
    description: str
    instructions: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class RecipeCreate(BaseModel):
    """Body of ``POST /api/recipes`` — author a CUSTOM recipe.

    ``name`` is not accepted: it is server-generated (opaque, unique
    per user). The human-facing title is ``display_name``.
    """

    display_name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1, max_length=1024)
    instructions: str = Field(..., min_length=1, max_length=8000)


class RecipeUpdate(BaseModel):
    """Body of ``PATCH /api/recipes/{id}`` — edit a CUSTOM recipe.

    Unlike a skill bundle, ``instructions`` are editable in place (plain
    text, no staging step). ``name`` is immutable. BUILTIN recipes are
    not editable through this surface (operator-managed).
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=1024)
    instructions: str | None = Field(default=None, min_length=1, max_length=8000)
    enabled: bool | None = None
