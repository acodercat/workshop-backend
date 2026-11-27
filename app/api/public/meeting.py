"""The meeting workspace: meetings hosted by the Meeting service, and the
recordings made here on their way to it.

Every endpoint authenticates the usual way. The hosted-meeting ones then
act at Meeting through the credential the sign-in holds, so the two can
never disagree about who the user is.
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_meeting_clients, get_redis
from app.api.uploads import read_capped_body
from app.auth import Principal, authenticate_session
from app.errors import (
    MeetingNotFoundError,
    MeetingServiceUnavailableError,
    RecordingTooLargeError,
    RecordingWindowInvalidError,
)
from app.schemas.file import UploadUrlResponse
from app.schemas.meeting import (
    HOSTED_PREFIX,
    AudioLink,
    CaptionLine,
    FlagUpdate,
    GeneratedText,
    LineTranslation,
    LiveSession,
    LiveStart,
    MediaRegistration,
    MediaUploadUrlRequest,
    MeetingDetail,
    MeetingPage,
    MeetingRename,
    MeetingView,
    Minutes,
    MinutesDraftRequest,
    MinutesRequest,
    MinutesUpdate,
    QuestionRequest,
    RecordingCreated,
    RecordingDetail,
    RecordingFinish,
    RecycleBinDeletion,
    RecycleBinDeletionResult,
    TranscriptLineUpdate,
    TranslationRequest,
)
from app.services.meeting import hosted, minutes, recordings
from app.services.meeting.clients import MeetingClients
from app.services.meeting.context import MeetingContext
from app.services.meeting.delegation import DelegationUnavailable, meeting_actor

router = APIRouter()


async def meeting_context(
    principal: Principal = Depends(authenticate_session),
    clients: MeetingClients = Depends(get_meeting_clients),
    redis: aioredis.Redis = Depends(get_redis),
) -> MeetingContext:
    try:
        delegation = await meeting_actor(principal.session_id)
    except DelegationUnavailable as exc:
        # The request authenticated, so the session exists; it just holds
        # nothing to act at Meeting with. Not the user's to fix.
        raise MeetingServiceUnavailableError("This session cannot reach Meeting.") from exc
    return MeetingContext(
        meetings=clients.meetings,
        actor=delegation.actor,
        account_id=delegation.account_id,
        session_id=principal.session_id,
        user_id=principal.user.id,
        redis=redis,
    )


def remote_meeting_id(meeting_id: str = Path(min_length=1, max_length=520)) -> str:
    """Meeting's own id for ``meeting-<id>``."""
    if not meeting_id.startswith(HOSTED_PREFIX) or meeting_id == HOSTED_PREFIX:
        raise MeetingNotFoundError("No such meeting.")
    return meeting_id.removeprefix(HOSTED_PREFIX)


#
# Hosted meetings


@router.get("/meetings")
async def list_meetings(
    view: MeetingView = "meetings",
    page: int = Query(default=1, ge=1),
    ctx: MeetingContext = Depends(meeting_context),
) -> MeetingPage:
    return await hosted.list_meetings(ctx, view=view, page=page)


@router.post("/meetings/recycle-bin/deletions")
async def delete_from_recycle_bin(
    body: RecycleBinDeletion,
    ctx: MeetingContext = Depends(meeting_context),
) -> RecycleBinDeletionResult:
    deleted = await hosted.delete_from_recycle_bin(ctx, ids=body.ids, page=body.page)
    return RecycleBinDeletionResult(deleted=deleted)


@router.get("/meetings/{meeting_id}")
async def get_meeting(
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> MeetingDetail:
    return await hosted.meeting_detail(ctx, remote_id)


@router.patch("/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def rename_meeting(
    body: MeetingRename,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> None:
    await hosted.rename(ctx, remote_id, title=body.title, revision=body.revision)


@router.put("/meetings/{meeting_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def set_favorite(
    body: FlagUpdate,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> None:
    await hosted.set_favorite(ctx, remote_id, body.enabled)


@router.put("/meetings/{meeting_id}/trashed", status_code=status.HTTP_204_NO_CONTENT)
async def set_trashed(
    body: FlagUpdate,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> None:
    await hosted.set_trashed(ctx, remote_id, body.enabled)


@router.get("/meetings/{meeting_id}/audio")
async def get_audio(
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> AudioLink:
    return AudioLink(url=await hosted.audio_url(ctx, remote_id))


@router.put("/meetings/{meeting_id}/minutes", status_code=status.HTTP_204_NO_CONTENT)
async def save_minutes(
    body: MinutesUpdate,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> None:
    await hosted.save_minutes(ctx, remote_id, content=body.content, revision=body.revision)


@router.post("/meetings/{meeting_id}/minutes")
async def get_minutes(
    body: MinutesRequest,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
    db: AsyncSession = Depends(get_db),
) -> Minutes:
    """The minutes, written first if there are none — which is why this is
    a POST: it can write to the record."""
    return await minutes.minutes(ctx, db, remote_id, language=body.language)


@router.put("/meetings/{meeting_id}/transcript/{index}", status_code=status.HTTP_204_NO_CONTENT)
async def save_transcript_line(
    body: TranscriptLineUpdate,
    index: int = Path(ge=0),
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
) -> None:
    await hosted.save_transcript_line(
        ctx,
        remote_id,
        index=index,
        content=body.content,
        speaker_name=body.speaker_name,
        revision=body.revision,
    )


@router.post("/meetings/{meeting_id}/translations")
async def translate_minutes(
    body: TranslationRequest,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
    db: AsyncSession = Depends(get_db),
) -> GeneratedText:
    content = await minutes.translate(
        ctx,
        db,
        remote_id,
        language=body.language,
        source_language=body.source_language,
        content=body.content,
    )
    return GeneratedText(content=content)


@router.post("/meetings/{meeting_id}/minutes-drafts")
async def draft_minutes(
    body: MinutesDraftRequest,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
    db: AsyncSession = Depends(get_db),
) -> GeneratedText:
    content = await minutes.draft(ctx, db, remote_id, template=body.template)
    return GeneratedText(content=content)


@router.post("/meetings/{meeting_id}/answers")
async def answer_question(
    body: QuestionRequest,
    remote_id: str = Depends(remote_meeting_id),
    ctx: MeetingContext = Depends(meeting_context),
    db: AsyncSession = Depends(get_db),
) -> GeneratedText:
    content = await minutes.answer(
        ctx, db, remote_id, messages=body.messages, language=body.language
    )
    return GeneratedText(content=content)


#
# Recordings: made here, archived to Meeting by the background process.


@router.post("/meeting-recordings/live", status_code=status.HTTP_201_CREATED)
async def start_live_recording(
    body: LiveStart,
    ctx: MeetingContext = Depends(meeting_context),
    db: AsyncSession = Depends(get_db),
) -> LiveSession:
    """Open Meeting's live interpretation and a recording to capture it.
    The browser connects to the returned stream itself for captions, and
    sends the audio and the finalized lines here."""
    return await recordings.start_live(ctx, db, body)


@router.post("/meeting-recordings/upload-urls")
async def create_recording_upload_url(
    body: MediaUploadUrlRequest,
    principal: Principal = Depends(authenticate_session),
) -> UploadUrlResponse:
    """Where the browser uploads a recording file directly to storage."""
    return recordings.upload_url(body)


@router.post("/meeting-recordings/uploads", status_code=status.HTTP_201_CREATED)
async def register_recording_upload(
    body: MediaRegistration,
    principal: Principal = Depends(authenticate_session),
    db: AsyncSession = Depends(get_db),
) -> RecordingCreated:
    """Queue an uploaded file to be handed to Meeting."""
    recording_id = await recordings.register_upload(
        db, principal.user.id, body, session_id=principal.session_id
    )
    return RecordingCreated(id=recording_id)


@router.get("/meeting-recordings/{recording_id}")
async def get_recording(
    recording_id: str,
    principal: Principal = Depends(authenticate_session),
    db: AsyncSession = Depends(get_db),
) -> RecordingDetail:
    return await recordings.detail(db, principal.user.id, recording_id)


@router.put(
    "/meeting-recordings/{recording_id}/audio/{seq}",
    status_code=status.HTTP_201_CREATED,
    response_class=Response,
    responses={200: {"description": "The same window was already stored."}},
)
async def append_recording_audio(
    request: Request,
    recording_id: str,
    seq: int = Path(ge=0),
    principal: Principal = Depends(authenticate_session),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """One window of 16 kHz mono 16-bit PCM, the raw body. 201 when stored,
    200 when it repeats a window already stored."""
    pcm = await read_capped_body(
        request,
        recordings.MAX_WINDOW_BYTES,
        too_large=RecordingTooLargeError("An audio window is at most ten seconds."),
    )
    if not pcm or len(pcm) % recordings.BYTES_PER_SAMPLE:
        raise RecordingWindowInvalidError("An audio window is whole 16-bit samples.")
    created = await recordings.append_audio(db, principal.user.id, recording_id, seq=seq, pcm=pcm)
    return Response(status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@router.put("/meeting-recordings/{recording_id}/lines/{seq}")
async def add_recording_line(
    body: CaptionLine,
    recording_id: str,
    seq: int = Path(ge=0, lt=100_000),
    ctx: MeetingContext = Depends(meeting_context),
    db: AsyncSession = Depends(get_db),
) -> LineTranslation:
    translation = await recordings.add_line(ctx, db, recording_id, seq=seq, line=body)
    return LineTranslation(translation=translation)


@router.post("/meeting-recordings/{recording_id}/finish", status_code=status.HTTP_202_ACCEPTED)
async def finish_recording(
    body: RecordingFinish,
    recording_id: str,
    principal: Principal = Depends(authenticate_session),
    db: AsyncSession = Depends(get_db),
) -> None:
    await recordings.finish(db, principal.user.id, recording_id, units=body.units)


@router.post("/meeting-recordings/{recording_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_recording(
    recording_id: str,
    principal: Principal = Depends(authenticate_session),
    db: AsyncSession = Depends(get_db),
) -> None:
    await recordings.retry(db, principal.user.id, recording_id, session_id=principal.session_id)
