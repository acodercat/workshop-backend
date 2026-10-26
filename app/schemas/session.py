from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.common.filenames import validate_input_filename
from app.models.session import AgentType, SessionStatus, Tier
from app.schemas.message_feedback import FeedbackResponse
from config import settings

# The cost is per configured Connector per turn — its tools enter the
# model's catalog on every step.
CONNECTORS_MAX = 20
# With the cap on *enabled* skills gone — turning one on lists it in the
# composer rather than loading it — an explicit pick is the only bound a
# user can push on how much skill metadata a turn puts in the model's
# context. The rest of it is the agent-only SYSTEM set, which only an
# operator adds to. Scheduled tasks pick from the same pool and pay the
# same per-turn cost, so ``app.schemas.schedule`` shares this bound.
SKILLS_MAX = 20


class SessionCreate(BaseModel):
    title: str | None = None
    # Capability tier; the backend resolves it to a model_id (the model name
    # is never sent over the wire). FAST / STANDARD / ADVANCED.
    tier: Tier = Tier.STANDARD
    # FULL = sandbox agent (default, back-compat); LITE = Q&A + web search.
    agent_type: AgentType = AgentType.FULL
    # Agent thinking toggle ("deep think" in the UI); default on.
    thinking_enabled: bool = True
    # What the session starts configured with; the ``?connector=`` deep
    # link is the one caller that arrives with any.
    connectors: list[int] = Field(default_factory=list, max_length=CONNECTORS_MAX)


class SessionUpdate(BaseModel):
    title: str | None = None


class SessionResponse(BaseModel):
    """Session metadata only — used by the list endpoint."""

    id: UUID
    user_id: int
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
    # No ``min_length``: an attachments-only turn (files but no typed text)
    # is a legal message — e.g. the user attaches a document, or a long
    # paste was diverted to a ``.txt`` attachment, and sends without typing.
    # ``_has_content`` below rejects the truly-empty case (no text, no
    # files). ``.strip()`` on the way in so whitespace-only isn't "content".
    content: str = Field(default="", max_length=100000)
    files: list[FileRef] = Field(
        default_factory=list,
        max_length=settings.max_message_files,
    )
    # Per-turn composer picks; see ``SKILLS_MAX`` for why this is bounded.
    skills: list[str] = Field(default_factory=list, max_length=SKILLS_MAX)
    # One-way LITE → FULL mode upgrade, committed atomically with this
    # send (the composer's mode pick is local until the user actually
    # sends). ``None`` = keep the session's current mode; FULL → LITE
    # is rejected. Unlike ``skills`` (a per-turn selection) this persists
    # on the session.
    agent_type: AgentType | None = None

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        return value.strip()

    @field_validator("files")
    @classmethod
    def _files_have_declared_sizes(cls, value: list[FileRef]) -> list[FileRef]:
        if any(file_ref.size_bytes is None for file_ref in value):
            raise ValueError("Each file must include size_bytes")
        return value

    @model_validator(mode="after")
    def _has_content(self) -> "MessageCreate":
        # A turn must carry something to act on: typed text or at least
        # one attachment. Runs after ``_strip_content``, so whitespace-only
        # text with no files is rejected.
        if not self.content and not self.files:
            raise ValueError("A message must have text or at least one file")
        return self


class MessageQueuedResponse(BaseModel):
    status: str = "queued"


class CancelAcceptedResponse(BaseModel):
    """Body of ``POST /sessions/{id}/cancel``.

    ``already_stopped`` is the whole point of having a body here: the
    request is accepted either way, but the caller cannot tell from the
    status code whether a worker was actually signalled or the turn had
    already finished. It needs to know, because the two cases end
    differently — a signalled worker sends ``agent.stopped`` over the
    stream and that is what releases the UI's running state, while a turn
    that was already over sends nothing ever again. Without this flag a
    client that had lost the stream would sit on a stop button that
    answers 202 and then does nothing.
    """

    already_stopped: bool = False


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
    # What the composer renders its switches from on open.
    connectors: list[int]
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]
    # Older messages exist above ``messages[0]``. Always False unless the
    # caller asked for a page: without ``turn_limit`` the payload is the
    # whole history and there is nothing above it.
    has_more_messages: bool = False

    model_config = {"from_attributes": True}


class MessagePageResponse(BaseModel):
    """One page of older messages, for scrolling back through a session.

    Same row shape as the detail payload so the client folds both through
    one path; ``has_more_messages`` reports whether the scroll can go
    further still."""

    messages: list[MessageResponse]
    has_more_messages: bool


class TitleGenerateRequest(BaseModel):
    """Body for ``POST /sessions/titles`` — content of the user's
    first message."""

    content: str = Field(..., min_length=1, max_length=200000)


class TitleGenerateResponse(BaseModel):
    """Response for ``POST /sessions/titles`` — LLM-generated
    conversation title."""

    title: str
