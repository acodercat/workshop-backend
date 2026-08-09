"""Response models for the console's session views.

Mirrors the customer-facing shapes (`app/schemas/session.py`) with two
support-driven differences: ``deleted_at`` is visible (the customer list
hides soft-deleted sessions; support is often asked about exactly those),
and messages carry no ``feedback`` field (feedback is the *requesting
user's* vote — meaningless for an operator).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.session import AgentType, SessionStatus, Tier


class SessionResponse(BaseModel):
    """One row of a user's session list, soft-deleted included."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    status: SessionStatus
    tier: Tier
    agent_type: AgentType
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class MessageResponse(BaseModel):
    """One message of a viewed session — role + rendered blocks."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    blocks: list[dict[str, Any]]
    created_at: datetime


class SessionDetailResponse(BaseModel):
    """A session as the console renders it: metadata + owner + history."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: int
    title: str | None
    status: SessionStatus
    tier: Tier
    agent_type: AgentType
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    messages: list[MessageResponse]
