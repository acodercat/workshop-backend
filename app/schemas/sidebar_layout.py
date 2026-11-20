"""Schemas for ``/me/sidebar-layout``: the order a user arranges their
sidebar in (``app.models.sidebar_layout``)."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounds a runaway client. The lists themselves stay far shorter: what they
# order is capped already, pinned sessions by ``MAX_PINNED_SESSIONS`` and
# projects per user.
SIDEBAR_ORDER_MAX = 200


class PinRef(BaseModel):
    """One item of the pinned group, where projects and sessions sort
    together."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["project", "session"]
    id: UUID


class SidebarLayoutUpdate(BaseModel):
    """Body of ``PUT /me/sidebar-layout``: the whole arrangement, replacing
    the one stored. An entry for something deleted or unpinned is allowed,
    and ignored: the client may save what it last saw."""

    pin_order: list[PinRef] = Field(..., max_length=SIDEBAR_ORDER_MAX)
    project_order: list[UUID] = Field(..., max_length=SIDEBAR_ORDER_MAX)

    @field_validator("pin_order", "project_order")
    @classmethod
    def _each_once(cls, value: list[PinRef] | list[UUID]) -> list[PinRef] | list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("An order lists each item at most once")
        return value


class SidebarLayoutResponse(BaseModel):
    """The caller's arrangement. Both lists are empty until their first
    save, which the client reads as "the default order"."""

    model_config = ConfigDict(from_attributes=True)

    pin_order: list[PinRef] = Field(default_factory=list)
    project_order: list[UUID] = Field(default_factory=list)
