"""Recordings on their way to becoming Meeting meetings.

A recording is made here — live, with Meeting's interpretation running
alongside, or by uploading a file — and *archived* to Meeting, which then
transcribes it and writes the minutes. From that point the meeting is a
hosted one (``archived_meeting_id``) and this row is only the record of how
it got there. Nothing about the finished meeting is stored here.

Lifecycle (``status``):

- ``RECORDING`` — live only: audio and caption lines are arriving.
- ``ARCHIVING`` — closed; waiting for, or in, the background archiver
  (``app/workers/meeting_archiver.py``).
- ``ARCHIVED`` — Meeting has it.
- ``FAILED`` — ``error`` says why. Retrying puts it back to ``ARCHIVING``,
  unless the error is final, in which case its audio has been let go.

A live recording that stops receiving audio without being finished is
closed by the archiver after ``meeting_abandoned_recording_hours`` and
archived with what arrived.

Archiving acts at Meeting as the user, through the sign-in session named by
``identity_session_id`` — never a copy of its credential. A recording whose
session has ended fails with ``meeting_session_expired`` and is retried
under the session that asks.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class RecordingKind(StrEnum):
    LIVE = "LIVE"
    UPLOAD = "UPLOAD"


class RecordingStatus(StrEnum):
    RECORDING = "RECORDING"
    ARCHIVING = "ARCHIVING"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class ArchiveState(StrEnum):
    """Whether Meeting has been asked to create the meeting.

    ``REQUESTED`` is written *before* the request and only cleared by its
    answer. A recording found ``REQUESTED`` by a later attempt had a request
    whose outcome nobody saw; repeating it could create the meeting twice,
    so it fails with ``meeting_archive_unknown`` instead.
    """

    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"
    DONE = "DONE"


class RecordingError(StrEnum):
    """Why a recording failed. The values are the client's localization
    keys, so they are lowercase like every other error code on the wire."""

    # Retryable: the cause can pass.
    SESSION_EXPIRED = "meeting_session_expired"
    RATE_LIMITED = "meeting_rate_limited"
    REFUSED = "meeting_refused"
    SERVICE_UNAVAILABLE = "meeting_service_unavailable"
    # Final: retrying cannot help, so the recording's audio is let go.
    NO_SPEECH = "no_speech"
    ARCHIVE_UNKNOWN = "meeting_archive_unknown"
    MEDIA_MISSING = "recording_media_missing"

    @property
    def final(self) -> bool:
        return self in _FINAL_ERRORS


_FINAL_ERRORS = frozenset(
    {RecordingError.NO_SPEECH, RecordingError.ARCHIVE_UNKNOWN, RecordingError.MEDIA_MISSING}
)


class CaptionTarget(StrEnum):
    """What a live recording's captions are translated into; ``auto`` picks
    per line."""

    AUTO = "auto"
    ZH_HK = "zh-HK"
    ZH_CN = "zh-CN"
    EN_US = "en-US"


def _enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, values_callable=lambda cls: [m.value for m in cls])


class MeetingRecording(Base):
    __tablename__ = "meeting_recording"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # ``SET NULL``: a signed-out session leaves the recording waiting for a
    # retry under a live one, rather than deleting the user's audio.
    identity_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("identity_session.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[RecordingKind] = mapped_column(
        _enum(RecordingKind, "meeting_recording_kind"), nullable=False
    )
    status: Mapped[RecordingStatus] = mapped_column(
        _enum(RecordingStatus, "meeting_recording_status"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    meeting_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Live only.
    target: Mapped[CaptionTarget | None] = mapped_column(
        _enum(CaptionTarget, "meeting_caption_target"), nullable=True
    )
    # Live: audio windows accepted (the next must be ``units``), and the
    # total PCM samples in them.
    units: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    samples: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # Upload: the file in object storage, until Meeting has it.
    media_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Upload: the recording's length, as the browser read it from the media.
    # (A live recording's is its sample count.)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_state: Mapped[ArchiveState] = mapped_column(
        _enum(ArchiveState, "meeting_archive_state"),
        nullable=False,
        server_default=ArchiveState.NOT_REQUESTED.value,
    )
    archived_meeting_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[RecordingError | None] = mapped_column(
        _enum(RecordingError, "meeting_recording_error"), nullable=True
    )
    # The archiver's claim: until ``lease_until``, the archiver holding
    # ``lease_token`` owns the row. Every write the archiver makes is
    # conditioned on its token, so one whose lease lapsed mid-way cannot
    # overwrite the archiver that took the row over.
    lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_meeting_recording_user", "user_id", "created_at"),
        # An uploaded object becomes at most one recording.
        Index(
            "uq_meeting_recording_media_key",
            "media_key",
            unique=True,
            postgresql_where=text("media_key IS NOT NULL"),
        ),
        # The archiver's queue.
        Index(
            "ix_meeting_recording_archiving",
            "lease_until",
            postgresql_where=text("status = 'ARCHIVING'"),
        ),
    )


class MeetingRecordingWindow(Base):
    """One window of live audio: 16 kHz mono 16-bit PCM, in order. Kept only
    until the recording is archived."""

    __tablename__ = "meeting_recording_window"

    recording_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("meeting_recording.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    pcm: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class MeetingRecordingLine(Base):
    """One finalized caption line of a live recording, and its translation
    (NULL until Meeting has translated it)."""

    __tablename__ = "meeting_recording_line"

    recording_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("meeting_recording.id", ondelete="CASCADE"), primary_key=True
    )
    # The caption's ordinal in the live stream. Not dense: ordinals are
    # also spent on partial captions that never become lines.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    translation: Mapped[str | None] = mapped_column(Text, nullable=True)
