from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.common.filenames import validate_input_filename
from app.common.timezones import is_known_timezone
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
# How many messages may wait in one session's queue. A queue is what a user
# typed ahead of one running turn, so the bound only has to stop a runaway
# client; each waiting message still costs nothing until it goes out.
QUEUED_MESSAGES_MAX = 20


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
    # Start the session in this project. Ownership is checked in the
    # endpoint.
    project_id: UUID | None = None

    @model_validator(mode="after")
    def _project_sessions_are_full(self) -> "SessionCreate":
        # The agent reads a project's files from its sandbox, which a LITE
        # session does not have. The composer never offers LITE in a
        # project, so this is a client bug rather than a choice to honour.
        if self.project_id is not None and self.agent_type is AgentType.LITE:
            raise ValueError("A session in a project runs as FULL")
        return self


class SessionUpdate(BaseModel):
    """Body of ``PATCH /sessions/{id}``.

    ``title``: ``None`` leaves it alone. ``project_id`` is read through
    ``model_fields_set``, because ``null`` means something there: sent as
    ``null`` it moves the session out of its project, sent as an id it moves
    the session into that project, and left out it changes nothing.
    ``pinned``: ``True`` pins the session, ``False`` unpins it, ``None``
    leaves it alone.
    """

    title: str | None = None
    project_id: UUID | None = None
    pinned: bool | None = None


class SessionResponse(BaseModel):
    """Session metadata only — used by the list endpoint."""

    id: UUID
    user_id: int
    title: str | None
    status: SessionStatus
    tier: Tier
    agent_type: AgentType
    project_id: UUID | None
    created_at: datetime
    updated_at: datetime
    # When the owner pinned the session; ``None`` = not pinned.
    pinned_at: datetime | None

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


class AnsweredQuestion(BaseModel):
    """One question of an ``ask_user`` card with the user's answer to it.

    Persisted inline on the user's text block under ``answers[]``, so the
    conversation draws the reply as questions and answers rather than as
    the text the model reads. An empty ``answer`` is a skipped question.
    """

    question: str = Field(..., min_length=1, max_length=2000)
    # The options picked, then any answer typed in their place: at most
    # every option of a question (``ask_user`` offers four) and one typed.
    answer: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=5
    )


def _answers_text(answers: list[AnsweredQuestion]) -> str:
    """The reply as the model reads it: each question, then the answer. The
    model has its own questions in context, so restating each one ties an
    answer to it without relying on order."""
    return "\n\n".join(
        f"{item.question}\n→ {', '.join(item.answer) or '(skipped)'}" for item in answers
    )


class MessageInput(BaseModel):
    """What a user message carries, whether it starts a turn now
    (:class:`MessageCreate`) or waits for one to end
    (:class:`QueuedMessageCreate`)."""

    # No ``min_length``: an attachments-only message (files but no typed
    # text) is legal — e.g. the user attaches a document, or a long paste
    # was diverted to a ``.txt`` attachment, and sends without typing. Each
    # subclass rejects the truly-empty case (no text, no files) itself.
    # ``.strip()`` on the way in so whitespace-only isn't "content".
    content: str = Field(default="", max_length=100000)
    files: list[FileRef] = Field(
        default_factory=list,
        max_length=settings.max_message_files,
    )
    # Per-turn composer picks; see ``SKILLS_MAX`` for why this is bounded.
    skills: list[str] = Field(default_factory=list, max_length=SKILLS_MAX)
    # The IANA zone the client is in, for a task the agent schedules on the
    # user's own clock. ``None`` = not reported, or not a zone this
    # deployment can compute with; the agent then has no ``create_schedule``
    # tool this turn.
    timezone: str | None = None

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def _drop_unknown_timezone(cls, value: str | None) -> str | None:
        # Dropped, not refused: where a message was sent from is no reason
        # to turn the message away.
        return value if value is not None and is_known_timezone(value) else None

    @field_validator("files")
    @classmethod
    def _files_have_declared_sizes(cls, value: list[FileRef]) -> list[FileRef]:
        if any(file_ref.size_bytes is None for file_ref in value):
            raise ValueError("Each file must include size_bytes")
        return value

    def _require_content(self) -> None:
        """A message must carry something to act on: typed text or at least
        one attachment. Called from each subclass's own validator rather
        than run here, because :class:`MessageCreate` derives its text
        first and a base class's validators run before a subclass's."""
        if not self.content and not self.files:
            raise ValueError("A message must have text or at least one file")


class MessageCreate(MessageInput):
    # One-way LITE → FULL mode upgrade, committed atomically with this
    # send (the composer's mode pick is local until the user actually
    # sends). ``None`` = keep the session's current mode; FULL → LITE
    # is rejected. Unlike ``skills`` (a per-turn selection) this persists
    # on the session.
    agent_type: AgentType | None = None
    # A reply to an ``ask_user`` card, one entry per question (a card asks
    # at most four). The answers are the message: ``content`` is derived
    # from them for the model, so a reply carries no text of its own.
    answers: list[AnsweredQuestion] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _answers_are_the_text(self) -> "MessageCreate":
        if self.answers:
            if self.content:
                raise ValueError("A reply to questions carries no text of its own")
            self.content = _answers_text(self.answers)
        return self

    @model_validator(mode="after")
    def _has_content(self) -> "MessageCreate":
        self._require_content()
        return self


class QueuedMessageCreate(MessageInput):
    """Body of ``POST /sessions/{id}/queued-messages``: a message sent
    while a turn runs, to go out once it ends."""

    @model_validator(mode="after")
    def _has_content(self) -> "QueuedMessageCreate":
        self._require_content()
        return self


class QueuedMessageUpdate(BaseModel):
    """Body of ``PATCH /sessions/{id}/queued-messages/{id}``: the message
    reworded. Its attachments and skill picks stay as they were sent."""

    # Stripped first, so whitespace alone is not text: the message keeps
    # its attachments, but rewording it to nothing is a delete, not an edit.
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100000)
    ]


class QueuedMessageOrder(BaseModel):
    """Body of ``PUT /sessions/{id}/queued-messages/order``: the queue's
    message ids in the order they should go out."""

    ids: list[int] = Field(..., max_length=QUEUED_MESSAGES_MAX)


class QueuedMessageResponse(BaseModel):
    """One waiting message, as the conversation lists it."""

    id: int
    content: str
    files: list[FileRef]
    skills: list[str]
    # Set while the queue is held: the dispatcher declined to send it on,
    # and nothing goes out until the session's next turn starts.
    held_at: datetime | None

    model_config = {"from_attributes": True}


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
    project_id: UUID | None
    # What the composer renders its switches from on open.
    connectors: list[int]
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]
    # Older messages exist above ``messages[0]``. Always False unless the
    # caller asked for a page: without ``turn_limit`` the payload is the
    # whole history and there is nothing above it.
    has_more_messages: bool = False
    # Messages sent while a turn was running, in the order they go out.
    queued_messages: list[QueuedMessageResponse] = Field(default_factory=list)

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
