from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.common.filenames import validate_input_filename
from app.models.session import AgentType, SessionStatus, Tier
from app.schemas.message_feedback import FeedbackResponse
from config import settings


class SessionCreate(BaseModel):
    title: str | None = None
    # Capability tier; the backend resolves it to a model_id (the model name
    # is never sent over the wire). FAST / STANDARD / ADVANCED.
    tier: Tier = Tier.STANDARD
    # FULL = sandbox agent (default, back-compat); LITE = Q&A + web search.
    agent_type: AgentType = AgentType.FULL
    # Agent thinking toggle ("deep think" in the UI); default on.
    thinking_enabled: bool = True


class SessionUpdate(BaseModel):
    title: str | None = None


class SessionResponse(BaseModel):
    """Session metadata only — used by the list endpoint."""
    id: UUID
    user_id: int | None
    title: str | None
    status: SessionStatus
    tier: Tier
    agent_type: AgentType
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
    size_bytes: int | None = Field(default=None, ge=0, le=settings.max_file_bytes)

    @field_validator("filename")
    @classmethod
    def _filename_is_safe_basename(cls, value: str) -> str:
        return validate_input_filename(value)


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=100000)
    files: list[FileRef] = Field(
        default_factory=list, max_length=settings.max_message_files,
    )
    skills: list[str] = Field(default_factory=list, max_length=20)
    # Optional recipe picked from the composer. The backend resolves it
    # to a pinned snapshot on ``session_turn.recipe``; its instructions
    # ride to the worker as a system-reminder. ``None`` = no recipe.
    recipe_id: int | None = Field(default=None, ge=1)
    # One-way LITE → FULL mode upgrade, committed atomically with this
    # send (the composer's mode pick is local until the user actually
    # sends). ``None`` = keep the session's current mode; FULL → LITE
    # is rejected. Unlike ``skills``/``recipe_id`` (per-turn selections)
    # this persists on the session.
    agent_type: AgentType | None = None

    @field_validator("files")
    @classmethod
    def _files_have_declared_sizes(cls, value: list[FileRef]) -> list[FileRef]:
        if any(file_ref.size_bytes is None for file_ref in value):
            raise ValueError("Each file must include size_bytes")
        return value


class MessageQueuedResponse(BaseModel):
    status: str = "queued"


class MessageResponse(BaseModel):
    """One message in the ``GET /sessions/{id}`` payload.

    ``feedback`` is the *current user's* feedback on this message
    (loaded by left-joining ``message_feedback`` filtered by the
    requesting ``user_id``). ``None`` means the user hasn't voted —
    not that no feedback exists in the table from anyone, since this
    is owner-only and there's only ever one voter per session anyway.
    Always ``None`` for user messages."""
    id: int
    role: str
    blocks: list[dict[str, Any]]
    created_at: datetime
    feedback: FeedbackResponse | None = None

    model_config = {"from_attributes": True}


class SessionDetailResponse(BaseModel):
    """Full session payload: metadata + status + structured message history."""
    id: UUID
    title: str | None
    status: SessionStatus
    tier: Tier
    agent_type: AgentType
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]

    model_config = {"from_attributes": True}


class TitleGenerateRequest(BaseModel):
    """Body for ``POST /sessions/titles`` — content of the user's
    first message."""
    content: str = Field(..., min_length=1, max_length=200000)


class TitleGenerateResponse(BaseModel):
    """Response for ``POST /sessions/titles`` — LLM-generated
    conversation title."""
    title: str
