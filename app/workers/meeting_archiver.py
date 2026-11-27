"""Handing closed recordings to Meeting.

Runs in the background process (:mod:`app.workers.run`). Each pass:

- **archives** recordings waiting in ``ARCHIVING``, one at a time. A row is
  claimed with ``FOR UPDATE SKIP LOCKED`` and a lease carrying a fresh
  token, and every write the archiver then makes to it is conditioned on
  that token — so an archiver whose lease lapsed mid-way (a slow upload,
  a paused process) finds its writes refused rather than overwriting the
  one that took the row over;
- **closes** live recordings abandoned mid-meeting, so what arrived is
  still archived.

A recording's local audio is dropped as soon as it is settled for good:
archived (Meeting has it) or failed with a final error (nobody ever will).

Archiving one recording:

1. act at Meeting as the user, through the sign-in session the recording
   names (one that has ended fails it, retryable under a live one);
2. assemble the audio — the live windows as one WAV, or the uploaded file
   from object storage;
3. put it into Meeting's storage and ask Meeting to file the meeting;
4. record the hosted meeting's id and drop the local audio.

Step 3's second half is the one that cannot be repeated blindly: if its
answer is lost, Meeting may or may not have filed the meeting. The intent
is committed first (``ArchiveState.REQUESTED``), conditioned on the claim,
so a recording found in that state was interrupted at the worst moment and
fails as ``meeting_archive_unknown`` instead of possibly filing a duplicate.
"""

import asyncio
import logging
import math
import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from app.database import AsyncSessionLocal
from app.integrations.meeting.storage import put_object
from app.integrations.meeting.transport import (
    MeetingDenied,
    MeetingError,
    MeetingRateLimited,
    MeetingRefused,
)
from app.models.meeting_recording import (
    ArchiveState,
    MeetingRecording,
    MeetingRecordingWindow,
    RecordingError,
    RecordingKind,
    RecordingStatus,
)
from app.services import file_storage
from app.services.identity.sessions import end_session
from app.services.meeting.clients import MeetingClients, open_meeting_clients
from app.services.meeting.delegation import DelegationUnavailable, meeting_actor
from app.services.meeting.recordings import BYTES_PER_SAMPLE, SAMPLE_RATE, media_extension
from config import settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
# Long enough for the largest recording (``meeting_max_upload_bytes``) to
# be uploaded and filed; a crashed archiver's claim lapses after this.
_LEASE = timedelta(minutes=15)


class _Failed(Exception):
    def __init__(self, error: RecordingError) -> None:
        super().__init__(error.value)
        self.error = error


class _LostClaim(Exception):
    """Another archiver has taken the row over; stop touching it."""


@dataclass(frozen=True)
class _Claim:
    recording_id: uuid.UUID
    token: uuid.UUID


async def run_forever() -> None:
    async with open_meeting_clients() as clients:
        while True:
            try:
                await _sweep()
                while (claim := await _claim()) is not None:
                    await _archive(clients, claim)
            except Exception:
                logger.exception("meeting archiver pass failed")
            await asyncio.sleep(POLL_SECONDS)


async def _claim() -> _Claim | None:
    """Take one recording waiting to be archived, or ``None``."""
    now = datetime.now(UTC)
    token = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        recording_id = (
            await db.execute(
                select(MeetingRecording.id)
                .where(
                    MeetingRecording.status == RecordingStatus.ARCHIVING,
                    (MeetingRecording.lease_until.is_(None)) | (MeetingRecording.lease_until < now),
                )
                .order_by(MeetingRecording.updated_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if recording_id is None:
            return None
        await db.execute(
            update(MeetingRecording)
            .where(MeetingRecording.id == recording_id)
            .values(lease_until=now + _LEASE, lease_token=token)
        )
        await db.commit()
    return _Claim(recording_id, token)


async def _archive(clients: MeetingClients, claim: _Claim) -> None:
    try:
        meeting_id = await _file(clients, claim)
    except _LostClaim:
        logger.warning("lost the claim on recording %s; leaving it", claim.recording_id)
        return
    except _Failed as failure:
        await _settle(claim, error=failure.error)
        return
    except Exception:
        logger.exception("archiving recording %s failed unexpectedly", claim.recording_id)
        await _settle(claim, error=RecordingError.SERVICE_UNAVAILABLE)
        return
    await _settle(claim, archived_meeting_id=meeting_id)


async def _file(clients: MeetingClients, claim: _Claim) -> str:
    async with AsyncSessionLocal() as db:
        row = await db.get(MeetingRecording, claim.recording_id)
    if row is None or row.lease_token != claim.token:
        raise _LostClaim
    if row.archive_state == ArchiveState.REQUESTED:
        raise _Failed(RecordingError.ARCHIVE_UNKNOWN)
    if row.identity_session_id is None:
        raise _Failed(RecordingError.SESSION_EXPIRED)
    try:
        delegation = await meeting_actor(row.identity_session_id)
    except DelegationUnavailable:
        raise _Failed(RecordingError.SESSION_EXPIRED) from None

    content, extension, file_name, duration = await _media(row)
    try:
        ticket = await clients.meetings.upload_ticket(delegation.actor)
        key = await put_object(ticket, content, extension=extension)
    except MeetingError as exc:
        raise await _meeting_failure(exc, row) from exc

    await _mark_requested(claim)
    try:
        return await clients.meetings.create_meeting(
            delegation.actor,
            name=row.title,
            remark=row.notes,
            time_sec=int(row.meeting_time.timestamp()),
            file_key=key,
            file_name=file_name,
            duration_seconds=duration,
        )
    except (MeetingDenied, MeetingRefused, MeetingRateLimited) as exc:
        # A definite answer: Meeting did not file it. Safe to try again.
        await _unmark_requested(claim)
        raise await _meeting_failure(exc, row) from exc
    except MeetingError as exc:
        # No usable answer. It may have been filed; leave ``REQUESTED`` standing.
        raise _Failed(RecordingError.ARCHIVE_UNKNOWN) from exc


async def _media(row: MeetingRecording) -> tuple[bytes, str, str, int]:
    """The recording's bytes, extension, file name, and length in seconds."""
    if row.kind == RecordingKind.LIVE:
        if row.samples == 0:
            raise _Failed(RecordingError.NO_SPEECH)
        async with AsyncSessionLocal() as db:
            windows = list(
                (
                    await db.execute(
                        select(MeetingRecordingWindow.pcm)
                        .where(MeetingRecordingWindow.recording_id == row.id)
                        .order_by(MeetingRecordingWindow.seq)
                    )
                ).scalars()
            )
        return wav(windows), ".wav", "meeting.wav", math.ceil(row.samples / SAMPLE_RATE)
    # An upload is registered with all three, and its key is only let go
    # once the recording is settled for good.
    assert row.media_key and row.media_name and row.duration_seconds
    try:
        content = await asyncio.to_thread(
            file_storage.download, file_storage.MEETING_MEDIA_POLICY, row.media_key
        )
    except Exception as exc:
        logger.warning("could not read upload for recording %s: %s", row.id, exc)
        raise _Failed(RecordingError.MEDIA_MISSING) from exc
    return content, media_extension(row.media_name), row.media_name, row.duration_seconds


def wav(windows: list[bytes]) -> bytes:
    """16 kHz mono 16-bit PCM windows as one WAV file, assembled in a single
    join so a recording at the size cap is not copied twice."""
    length = sum(len(window) for window in windows)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + length,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        SAMPLE_RATE,
        SAMPLE_RATE * BYTES_PER_SAMPLE,
        BYTES_PER_SAMPLE,
        8 * BYTES_PER_SAMPLE,
        b"data",
        length,
    )
    return b"".join([header, *windows])


async def _meeting_failure(exc: MeetingError, row: MeetingRecording) -> _Failed:
    if isinstance(exc, MeetingDenied):
        # Meeting has ended the session behind this sign-in; end it here
        # too, as a request would.
        if row.identity_session_id is not None:
            await end_session(row.identity_session_id)
        return _Failed(RecordingError.SESSION_EXPIRED)
    if isinstance(exc, MeetingRateLimited):
        return _Failed(RecordingError.RATE_LIMITED)
    if isinstance(exc, MeetingRefused):
        logger.info("meeting refused to archive recording %s: %s", row.id, exc.code)
        return _Failed(RecordingError.REFUSED)
    return _Failed(RecordingError.SERVICE_UNAVAILABLE)


def _owned_by(claim: _Claim):
    return (
        MeetingRecording.id == claim.recording_id,
        MeetingRecording.lease_token == claim.token,
    )


async def _mark_requested(claim: _Claim) -> None:
    """Commit the intent to file, if the claim still holds. The condition
    on ``NOT_REQUESTED`` makes this the point only one archiver can pass."""
    async with AsyncSessionLocal() as db:
        marked = (
            await db.execute(
                update(MeetingRecording)
                .where(
                    *_owned_by(claim),
                    MeetingRecording.archive_state == ArchiveState.NOT_REQUESTED,
                )
                .values(archive_state=ArchiveState.REQUESTED)
                .returning(MeetingRecording.id)
            )
        ).scalar_one_or_none()
        await db.commit()
    if marked is None:
        raise _LostClaim


async def _unmark_requested(claim: _Claim) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MeetingRecording)
            .where(*_owned_by(claim))
            .values(archive_state=ArchiveState.NOT_REQUESTED)
        )
        await db.commit()


async def _settle(
    claim: _Claim,
    *,
    error: RecordingError | None = None,
    archived_meeting_id: str | None = None,
) -> None:
    """Record the outcome and release the claim — if it is still ours."""
    now = datetime.now(UTC)
    values: dict[str, object] = {"lease_until": None, "lease_token": None, "updated_at": now}
    if archived_meeting_id is not None:
        values |= {
            "status": RecordingStatus.ARCHIVED,
            "archive_state": ArchiveState.DONE,
            "archived_meeting_id": archived_meeting_id,
            "error": None,
        }
    else:
        values |= {"status": RecordingStatus.FAILED, "error": error}
    async with AsyncSessionLocal() as db:
        settled = (
            await db.execute(
                update(MeetingRecording)
                .where(*_owned_by(claim))
                .values(**values)
                .returning(MeetingRecording.id)
            )
        ).scalar_one_or_none()
        await db.commit()
    if settled is None:
        logger.warning("lost the claim on recording %s before settling", claim.recording_id)
        return
    if archived_meeting_id is not None or (error is not None and error.final):
        # Meeting has the audio, or never will: this copy has served its purpose.
        await _release_audio(claim.recording_id)


async def _release_audio(recording_id: uuid.UUID) -> None:
    """Drop the recording's local audio: its live windows, or its upload."""
    async with AsyncSessionLocal() as db:
        media_key = (
            await db.execute(
                select(MeetingRecording.media_key)
                .where(MeetingRecording.id == recording_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        await db.execute(
            update(MeetingRecording)
            .where(MeetingRecording.id == recording_id)
            .values(media_key=None)
        )
        await db.execute(
            delete(MeetingRecordingWindow).where(
                MeetingRecordingWindow.recording_id == recording_id
            )
        )
        await db.commit()
    if media_key:
        try:
            await asyncio.to_thread(
                file_storage.delete, file_storage.MEETING_MEDIA_POLICY, media_key
            )
        except Exception:
            # The row no longer points at it; the bucket keeps an orphan,
            # which costs storage, not correctness.
            logger.warning("could not delete released upload %s", media_key)


async def _sweep() -> None:
    """Close live recordings abandoned mid-meeting — the browser closed, the
    network died — so what arrived is archived rather than kept forever."""
    cutoff = datetime.now(UTC) - timedelta(hours=settings.meeting_abandoned_recording_hours)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MeetingRecording)
            .where(
                MeetingRecording.status == RecordingStatus.RECORDING,
                MeetingRecording.updated_at < cutoff,
            )
            .values(status=RecordingStatus.ARCHIVING, updated_at=datetime.now(UTC))
        )
        await db.commit()
