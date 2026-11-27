"""Making recordings — live or uploaded — that are archived to Meeting.

The request-side half: starting a live recording, taking its audio and
caption lines, closing it, registering an upload, retrying a failure, and
reading a recording's state. Handing the closed recording to Meeting is
the background half, :mod:`app.workers.meeting_archiver`.
"""

import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import (
    FileNotUploadedError,
    RecordingConflictError,
    RecordingMediaUnsupportedError,
    RecordingNotFoundError,
    RecordingTooLargeError,
)
from app.integrations.meeting.output import OutputError
from app.integrations.meeting.transport import (
    MeetingRateLimited,
    MeetingRefused,
    MeetingUnavailable,
)
from app.models.meeting_recording import (
    MeetingRecording,
    MeetingRecordingLine,
    MeetingRecordingWindow,
    RecordingKind,
    RecordingStatus,
)
from app.schemas.file import UploadUrlResponse
from app.schemas.meeting import (
    HOSTED_PREFIX,
    CaptionLine,
    LiveSession,
    LiveStart,
    LiveStream,
    MediaRegistration,
    MediaUploadUrlRequest,
    RecordingDetail,
    TranscriptLine,
)
from app.services import file_storage
from app.services.meeting.context import MeetingContext, meeting_errors
from config import settings

# 16 kHz mono 16-bit PCM.
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
# The largest window the browser sends: ten seconds.
MAX_WINDOW_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * 10
# A caption's translation is a convenience during the meeting: when Meeting
# cannot give one now, the line is kept without it.
_TRANSLATION_CAN_WAIT = (MeetingUnavailable, MeetingRefused, MeetingRateLimited, OutputError)


def _parse_id(recording_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(recording_id)
    except ValueError:
        raise RecordingNotFoundError("No such recording.") from None


async def _owned(
    db: AsyncSession,
    user_id: int,
    recording_id: str,
    *,
    kind: RecordingKind | None = None,
    for_update: bool = False,
) -> MeetingRecording:
    query = select(MeetingRecording).where(
        MeetingRecording.id == _parse_id(recording_id), MeetingRecording.user_id == user_id
    )
    if kind is not None:
        query = query.where(MeetingRecording.kind == kind)
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise RecordingNotFoundError("No such recording.")
    return row


async def detail(db: AsyncSession, user_id: int, recording_id: str) -> RecordingDetail:
    row = await _owned(db, user_id, recording_id)
    lines = (
        await db.execute(
            select(MeetingRecordingLine)
            .where(MeetingRecordingLine.recording_id == row.id)
            .order_by(MeetingRecordingLine.seq)
        )
    ).scalars()
    return RecordingDetail(
        id=str(row.id),
        kind=row.kind,
        status=row.status,
        title=row.title,
        time=row.meeting_time,
        notes=row.notes,
        error=row.error,
        units=row.units,
        target=row.target,
        transcript=[
            TranscriptLine(
                speaker="",
                text=line.text,
                translation=line.translation or "",
                start_ms=line.start_ms,
            )
            for line in lines
        ],
        archived_meeting_id=(
            HOSTED_PREFIX + row.archived_meeting_id if row.archived_meeting_id else None
        ),
        retryable=row.status == RecordingStatus.FAILED
        and row.error is not None
        and not row.error.final,
    )


#
# Live


async def start_live(ctx: MeetingContext, db: AsyncSession, body: LiveStart) -> LiveSession:
    """Open Meeting's live stream and the recording that will capture it.

    Meeting first: it cannot be undone as part of the local transaction, so
    a failure there leaves nothing behind here, while the reverse order
    would leave a recording with no stream.
    """
    meeting_time = body.meeting_time or datetime.now(UTC)
    async with meeting_errors(ctx):
        stream = await ctx.meetings.create_streaming(
            ctx.actor,
            creator_id=ctx.account_id,
            name=body.title,
            remark=body.notes,
            time_sec=int(meeting_time.timestamp()),
        )
    row = MeetingRecording(
        user_id=ctx.user_id,
        identity_session_id=ctx.session_id,
        kind=RecordingKind.LIVE,
        status=RecordingStatus.RECORDING,
        title=body.title,
        notes=body.notes,
        meeting_time=meeting_time,
        target=body.target,
    )
    db.add(row)
    await db.commit()
    return LiveSession(
        id=str(row.id),
        stream=LiveStream(meeting_id=stream.meeting_id, websocket_url=stream.websocket_url),
    )


async def append_audio(
    db: AsyncSession, user_id: int, recording_id: str, *, seq: int, pcm: bytes
) -> bool:
    """Store one audio window. Returns whether it was new.

    Windows must arrive in order. A window sent again — a retry after a
    lost response — is accepted if it is the same bytes, and refused if
    it is not, because then two different recordings claim one position.
    """
    row = await _owned(db, user_id, recording_id, kind=RecordingKind.LIVE, for_update=True)
    if seq < row.units:
        stored = (
            await db.execute(
                select(MeetingRecordingWindow.pcm).where(
                    MeetingRecordingWindow.recording_id == row.id,
                    MeetingRecordingWindow.seq == seq,
                )
            )
        ).scalar_one_or_none()
        if stored != pcm:
            raise RecordingConflictError("A different window was already stored here.")
        return False
    if row.status != RecordingStatus.RECORDING or seq != row.units:
        raise RecordingConflictError("The recording is closed, or a window is missing.")
    if row.samples * BYTES_PER_SAMPLE + len(pcm) > settings.meeting_max_upload_bytes:
        raise RecordingTooLargeError("The recording has reached its size limit.")
    db.add(MeetingRecordingWindow(recording_id=row.id, seq=seq, pcm=pcm))
    row.units += 1
    row.samples += len(pcm) // BYTES_PER_SAMPLE
    row.updated_at = datetime.now(UTC)
    await db.commit()
    return True


async def add_line(
    ctx: MeetingContext, db: AsyncSession, recording_id: str, *, seq: int, line: CaptionLine
) -> str | None:
    """Store a finalized caption line and return its translation, or
    ``None`` if Meeting could not translate it now.

    Idempotent by ``seq``; the same position with different content is a
    conflict. Translation failures do not fail the request — the caption
    is what matters during a meeting — except a lost session, which would
    fail every later request too.
    """
    row = await _owned(db, ctx.user_id, recording_id, kind=RecordingKind.LIVE)
    if row.status != RecordingStatus.RECORDING or row.target is None:
        raise RecordingConflictError("The recording is closed.")
    target = row.target
    await db.execute(
        insert(MeetingRecordingLine)
        .values(recording_id=row.id, seq=seq, start_ms=line.start_ms, text=line.text)
        .on_conflict_do_nothing()
    )
    stored = (
        await db.execute(
            select(MeetingRecordingLine).where(
                MeetingRecordingLine.recording_id == row.id, MeetingRecordingLine.seq == seq
            )
        )
    ).scalar_one()
    await db.commit()
    if stored.text != line.text or stored.start_ms != line.start_ms:
        raise RecordingConflictError("A different line was already stored here.")
    if stored.translation:
        return stored.translation

    async with meeting_errors(ctx):
        try:
            translation = await ctx.meetings.translate_line(
                ctx.actor, text=line.text, target=target.value
            )
        except _TRANSLATION_CAN_WAIT:
            return None
    await db.execute(
        update(MeetingRecordingLine)
        .where(MeetingRecordingLine.recording_id == row.id, MeetingRecordingLine.seq == seq)
        .values(translation=translation)
    )
    await db.commit()
    return translation


async def finish(db: AsyncSession, user_id: int, recording_id: str, *, units: int) -> None:
    """Close a live recording and queue it for archiving. ``units`` must be
    exactly the windows that arrived, so nothing the browser recorded is
    silently missing from what Meeting receives."""
    row = await _owned(db, user_id, recording_id, kind=RecordingKind.LIVE, for_update=True)
    if row.status != RecordingStatus.RECORDING or row.units != units:
        raise RecordingConflictError("The recording is closed, or audio is missing.")
    row.status = RecordingStatus.ARCHIVING
    row.updated_at = datetime.now(UTC)
    await db.commit()


async def retry(
    db: AsyncSession, user_id: int, recording_id: str, *, session_id: uuid.UUID
) -> None:
    """Queue a failed recording again, acting as the session that asks —
    the one it had may be what failed."""
    row = await _owned(db, user_id, recording_id, for_update=True)
    if row.status != RecordingStatus.FAILED or row.error is None or row.error.final:
        raise RecordingConflictError("This recording cannot be retried.")
    row.status = RecordingStatus.ARCHIVING
    row.error = None
    row.lease_until = None
    row.lease_token = None
    row.identity_session_id = session_id
    row.updated_at = datetime.now(UTC)
    await db.commit()


#
# Upload


def media_extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def _require_media(filename: str) -> None:
    if media_extension(filename) not in file_storage.MEETING_MEDIA_POLICY.allowed_types:
        raise RecordingMediaUnsupportedError("Only audio and video recordings can be uploaded.")


def upload_url(body: MediaUploadUrlRequest) -> UploadUrlResponse:
    """Where the browser uploads a recording file directly to storage."""
    _require_media(body.filename)
    return file_storage.generate_upload_url(
        body.filename, body.size_bytes, file_storage.MEETING_MEDIA_POLICY
    )


async def register_upload(
    db: AsyncSession, user_id: int, body: MediaRegistration, *, session_id: uuid.UUID
) -> str:
    """Turn an uploaded object into a recording queued for archiving.

    The object must exist and fit the size cap (read back from storage —
    the browser's claim is not trusted), and its orphan tag is cleared
    *before* the row is committed: a recording must never exist whose file
    the bucket's cleanup rule may still delete. Clearing is idempotent, so
    a retry after a failure here is safe.
    """
    _require_media(body.filename)
    policy = file_storage.MEETING_MEDIA_POLICY
    if not body.file_key.startswith(policy.key_prefix + "/"):
        raise FileNotUploadedError("That file was not uploaded as a recording.")
    await file_storage.confirmed_sizes(policy, [body.file_key])
    await file_storage.clear_orphan_tags(policy, [body.file_key])
    recording_id = (
        await db.execute(
            insert(MeetingRecording)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                identity_session_id=session_id,
                kind=RecordingKind.UPLOAD,
                status=RecordingStatus.ARCHIVING,
                title=body.title,
                notes=body.notes,
                meeting_time=body.meeting_time or datetime.now(UTC),
                media_key=body.file_key,
                media_name=body.filename,
                duration_seconds=body.duration_seconds,
            )
            .on_conflict_do_nothing()
            .returning(MeetingRecording.id)
        )
    ).scalar_one_or_none()
    await db.commit()
    if recording_id is None:
        raise RecordingConflictError("This upload is already a recording.")
    return str(recording_id)
