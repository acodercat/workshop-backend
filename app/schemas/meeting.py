"""Wire shapes for the meeting workspace.

Two resources:

- **Meetings** (id ``meeting-<id>``) are hosted by the Meeting service and
  read and edited there as the user — :class:`MeetingDetail`.
- **Recordings** (a UUID) are made here, live or by upload, and archived to
  Meeting, after which they are a meeting like any other —
  :class:`RecordingDetail` names the meeting they became.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.models.meeting_recording import (
    CaptionTarget,
    RecordingError,
    RecordingKind,
    RecordingStatus,
)
from app.schemas.meeting_library import TemplateDraft

HOSTED_PREFIX = "meeting-"

MeetingView = Literal["meetings", "favorites", "recycleBin"]
MeetingStatus = Literal["processing", "complete", "failed"]
# The three languages minutes are kept in: Traditional, Simplified, English.
MinutesLanguage = Literal["tc", "sc", "en"]

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MinutesVariants(BaseModel):
    tc: str
    sc: str
    en: str


class MeetingSummary(BaseModel):
    id: str
    title: str
    # ``None`` when Meeting gave no time it could be read from.
    time: datetime | None
    summary: str
    # The summary in each language, where Meeting provides it.
    summaries: MinutesVariants
    status: MeetingStatus
    favorite: bool
    trashed: bool


class MeetingPage(BaseModel):
    items: list[MeetingSummary]
    total: int
    page: int


class TranscriptLine(BaseModel):
    speaker: str
    text: str
    translation: str
    start_ms: int


class HostedRecord(BaseModel):
    overviews: MinutesVariants
    # Opaque fingerprint of the record as read. Every edit sends back the
    # one it was made against; a mismatch means someone else changed it.
    revision: str
    # False until Meeting has a recognition to write transcript edits and
    # minutes to.
    editable: bool


class MeetingDetail(MeetingSummary):
    transcript: list[TranscriptLine]
    remote: HostedRecord


class FlagUpdate(BaseModel):
    enabled: bool


def _distinct(ids: list[str]) -> list[str]:
    if len(set(ids)) != len(ids):
        raise ValueError("the same meeting is listed twice")
    return ids


class RecycleBinDeletion(BaseModel):
    """Permanently delete meetings from the recycle bin. ``page`` is the
    recycle-bin page the ids were chosen from; every id must still be on it,
    in the bin, or nothing is deleted."""

    ids: Annotated[
        list[Annotated[str, StringConstraints(pattern=f"^{HOSTED_PREFIX}.+", max_length=520)]],
        Field(min_length=1, max_length=20),
        AfterValidator(_distinct),
    ]
    page: int = Field(ge=1)


class RecycleBinDeletionResult(BaseModel):
    deleted: int


class AudioLink(BaseModel):
    # Empty when Meeting has no recording for the meeting.
    url: str


class _Revisioned(BaseModel):
    revision: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class MeetingRename(_Revisioned):
    title: Annotated[_Text, StringConstraints(max_length=80)]


class MinutesUpdate(_Revisioned):
    content: Annotated[_Text, StringConstraints(max_length=200_000)]


class TranscriptLineUpdate(_Revisioned):
    content: Annotated[_Text, StringConstraints(max_length=100_000)]
    speaker_name: Annotated[_Text, StringConstraints(max_length=200)]


class MinutesRequest(BaseModel):
    language: MinutesLanguage


class Minutes(BaseModel):
    language: MinutesLanguage
    content: str
    revision: str
    # The same minutes in the other Chinese script, where it can be derived.
    variants: dict[MinutesLanguage, str]


class TranslationRequest(BaseModel):
    language: MinutesLanguage
    source_language: MinutesLanguage | None = None
    # Not needed when the record already holds minutes in ``language``.
    content: Annotated[str, StringConstraints(max_length=200_000)] = ""


class GeneratedText(BaseModel):
    content: str


class MinutesDraftRequest(BaseModel):
    """Draft minutes, optionally to a template (which the model follows)."""

    template: TemplateDraft | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Annotated[_Text, StringConstraints(max_length=20_000)]


class QuestionRequest(BaseModel):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=60)]
    language: MinutesLanguage


#
# Recordings

_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
_Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50_000)]


class RecordingDetail(BaseModel):
    id: str
    kind: RecordingKind
    status: RecordingStatus
    title: str
    time: datetime
    notes: str
    error: RecordingError | None
    # Live: audio windows accepted so far; the next must be numbered this.
    units: int
    target: CaptionTarget | None
    # Live: the caption lines and their translations.
    transcript: list[TranscriptLine]
    # The meeting it became (``meeting-<id>``), once archived.
    archived_meeting_id: str | None
    # Whether retrying can help: a lost session can, no speech cannot.
    retryable: bool


class LiveStart(BaseModel):
    title: _Title
    notes: _Notes = ""
    meeting_time: datetime | None = None
    target: CaptionTarget


class LiveStream(BaseModel):
    """Meeting's live interpretation stream, which the browser connects to
    directly for captions."""

    meeting_id: str
    websocket_url: str


class LiveSession(BaseModel):
    id: str
    stream: LiveStream


class CaptionLine(BaseModel):
    start_ms: int = Field(ge=0)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]


class LineTranslation(BaseModel):
    # ``None`` while Meeting has not translated it; the line is kept.
    translation: str | None


class RecordingFinish(BaseModel):
    # The number of audio windows the client sent. Must match what arrived,
    # or audio would be missing from what is archived.
    units: int = Field(ge=0)


class MediaUploadUrlRequest(BaseModel):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    size_bytes: int = Field(gt=0)


class MediaRegistration(BaseModel):
    file_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    title: _Title
    notes: _Notes = ""
    meeting_time: datetime | None = None
    # Read by the browser from the media itself; Meeting files a recording
    # with its length.
    duration_seconds: int = Field(gt=0, le=24 * 3600)


class RecordingCreated(BaseModel):
    id: str
