"""Schemas for the /api/actors surface (user-owned AI worker identities)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ActorResponse(BaseModel):
    """One actor rendered for the list / detail surfaces.

    ``instructions`` is included so the management UI can show and edit
    the charter. ``avatar_key`` is the raw OSS key (NULL = client
    derives a generated avatar from ``id``); URL minting arrives with
    the avatar-upload flow and is deliberately not baked in here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    instructions: str
    avatar_key: str | None
    created_at: datetime
    updated_at: datetime


class ActorCreate(BaseModel):
    """Body of ``POST /api/actors``.

    ``name`` is user-chosen (it *is* the identity — unlike a recipe
    there is no server-generated slug) and unique per owner among live
    actors. ``avatar_key`` is not accepted: the avatar rides its own
    upload flow, which owns key minting and object cleanup.
    """

    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1024)
    instructions: str = Field(..., min_length=1, max_length=8000)


class ActorUpdate(BaseModel):
    """Body of ``PATCH /api/actors/{id}`` — every identity field is
    mutable in place (plain text, no staging step — the recipe posture,
    not the skill-bundle one). Edits never reach already-queued turns:
    once sessions run under actors, each turn will carry its
    enqueue-time actor snapshot (the ``session_turn.recipe`` pattern).
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1024)
    instructions: str | None = Field(default=None, min_length=1, max_length=8000)
