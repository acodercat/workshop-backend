from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.session import SessionStatus


class SessionCreate(BaseModel):
    title: str | None = None
    model_id: str | None = None


class SessionUpdate(BaseModel):
    title: str | None = None


class SessionResponse(BaseModel):
    """Session metadata only — used by the list endpoint."""
    id: UUID
    user_id: int | None
    title: str | None
    status: SessionStatus
    model_id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FileRef(BaseModel):
    """Reference to a file in OSS.

    Frontend uploads bytes directly to OSS (via a presigned URL) and
    posts only the resulting ``file_key`` here. Persisted inline on the
    user's text block under ``files[]``; also forwarded to the worker,
    which pulls bytes from OSS into the sandbox before the agent runs.
    """
    file_key: str = Field(..., min_length=1, max_length=512)
    filename: str = Field(..., min_length=1, max_length=255)
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    files: list[FileRef] = Field(default_factory=list)


class MessageQueuedResponse(BaseModel):
    status: str = "queued"


class MessageResponse(BaseModel):
    """One message in the ``GET /sessions/{id}`` payload."""
    id: int
    role: str
    blocks: list[dict[str, Any]]
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionDetailResponse(BaseModel):
    """Full session payload: metadata + status + structured message history."""
    id: UUID
    title: str | None
    status: SessionStatus
    model_id: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]

    model_config = {"from_attributes": True}


class TitleGenerateRequest(BaseModel):
    """Body for ``POST /sessions/titles`` — content of the user's
    first message."""
    content: str = Field(..., min_length=1, max_length=10000)


class TitleGenerateResponse(BaseModel):
    """Response for ``POST /sessions/titles`` — LLM-generated
    conversation title."""
    title: str
