"""Meetings hosted by the Meeting service, read and edited as the user.

Meeting is the record of truth for these: nothing about them is stored
here. Every read goes to Meeting, and every edit is a compare-and-swap
against the record's *revision* — a fingerprint of the fields an edit can
touch — followed by a read-back that confirms the change landed. Meeting
has no conditional write of its own, so the check-then-write is guarded by
a short lock per meeting to keep two of our own requests from interleaving
(an edit made in Meeting's own app in that window is still possible, and
is what the read-back is for).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.errors import (
    MeetingChangedError,
    MeetingNotEditableError,
    MeetingSaveUnconfirmedError,
)
from app.integrations.meeting.meetings import PAGE_SIZE, ListFilter
from app.integrations.meeting.records import MeetingListing, MeetingRecord, Overviews
from app.schemas.meeting import (
    HOSTED_PREFIX,
    HostedRecord,
    MeetingDetail,
    MeetingPage,
    MeetingSummary,
    MeetingView,
    MinutesVariants,
    TranscriptLine,
)
from app.services.meeting.context import MeetingContext, meeting_errors
from app.services.redis import LockHeld, short_lock

_FILTERS: dict[MeetingView, ListFilter] = {
    "meetings": "all",
    "favorites": "favorites",
    "recycleBin": "trash",
}
# Long enough to cover a read, a write and a read-back at Meeting's
# ordinary timeout; short enough that a crashed request does not lock the
# record out for long.
_EDIT_LOCK_SECONDS = 150


def _variants(overviews: Overviews) -> MinutesVariants:
    return MinutesVariants(tc=overviews.tc, sc=overviews.sc, en=overviews.en)


def _summary(listing: MeetingListing) -> MeetingSummary:
    return MeetingSummary(
        id=HOSTED_PREFIX + listing.meeting_id,
        title=listing.title,
        time=listing.time,
        summary=listing.summary,
        summaries=_variants(listing.summaries),
        status=listing.status,
        favorite=listing.favorite,
        trashed=listing.trashed,
    )


def _detail(record: MeetingRecord) -> MeetingDetail:
    overviews = record.overviews
    return MeetingDetail(
        **{
            **_summary(record).model_dump(),
            "summary": overviews.tc or overviews.en or overviews.sc,
        },
        transcript=[
            TranscriptLine(
                speaker=segment.speaker,
                text=segment.text,
                translation=segment.translation,
                start_ms=segment.start_ms,
            )
            for segment in record.transcript
        ],
        remote=HostedRecord(
            overviews=_variants(overviews),
            revision=record.revision,
            editable=record.editable,
        ),
    )


async def list_meetings(ctx: MeetingContext, *, view: MeetingView, page: int) -> MeetingPage:
    """One page of the user's meetings. A page past the end answers with
    the last page, so a deletion that shortens the list does not strand
    the client on an empty one."""
    async with meeting_errors(ctx):
        listed = await ctx.meetings.list(ctx.actor, which=_FILTERS[view], page_index=page - 1)
    last = max(1, -(-listed.total // PAGE_SIZE))
    if page > last and listed.total > 0:
        return await list_meetings(ctx, view=view, page=last)
    return MeetingPage(
        items=[_summary(item) for item in listed.items],
        total=listed.total,
        page=min(last, page),
    )


async def read(ctx: MeetingContext, remote_id: str) -> MeetingRecord:
    async with meeting_errors(ctx):
        return await ctx.meetings.info(ctx.actor, remote_id)


async def meeting_detail(ctx: MeetingContext, remote_id: str) -> MeetingDetail:
    return _detail(await read(ctx, remote_id))


async def set_favorite(ctx: MeetingContext, remote_id: str, enabled: bool) -> None:
    async with meeting_errors(ctx):
        await ctx.meetings.set_favorite(ctx.actor, remote_id, enabled)


async def set_trashed(ctx: MeetingContext, remote_id: str, enabled: bool) -> None:
    async with meeting_errors(ctx):
        await ctx.meetings.set_trash(ctx.actor, remote_id, enabled)


async def delete_from_recycle_bin(ctx: MeetingContext, *, ids: list[str], page: int) -> int:
    """Permanently delete meetings the user picked from the recycle bin.

    Every id must still be on the page it was picked from and still in the
    bin — the list may have changed since — or nothing is deleted. Meeting
    deletes one meeting per call and has no batch, so a failure part-way
    leaves the earlier ones deleted; the error says so by being raised.
    """
    trash = await list_meetings(ctx, view="recycleBin", page=page)
    in_bin = {item.id for item in trash.items if item.trashed}
    if not set(ids) <= in_bin:
        raise MeetingChangedError("The recycle bin changed; reload and select again.")
    async with meeting_errors(ctx):
        for meeting_id in ids:
            await ctx.meetings.delete(ctx.actor, meeting_id.removeprefix(HOSTED_PREFIX))
    return len(ids)


async def audio_url(ctx: MeetingContext, remote_id: str) -> str:
    """A signed link to the meeting's recording, or ``""`` if it has none."""
    async with meeting_errors(ctx):
        return await ctx.meetings.audio_url(ctx.actor, remote_id)


@asynccontextmanager
async def _editing(
    ctx: MeetingContext, remote_id: str, revision: str
) -> AsyncIterator[MeetingRecord]:
    """Hold the record's edit lock and check it is still at ``revision``."""
    try:
        async with short_lock(
            ctx.redis, f"meeting_edit:{ctx.user_id}:{remote_id}", ttl_seconds=_EDIT_LOCK_SECONDS
        ):
            current = await read(ctx, remote_id)
            if current.revision != revision:
                raise MeetingChangedError("The meeting changed since it was read.")
            yield current
    except LockHeld:
        raise MeetingChangedError("Another edit of this meeting is in progress.") from None


def _require_editable(record: MeetingRecord) -> None:
    if not record.editable:
        raise MeetingNotEditableError("This meeting has no transcript record yet.")


async def rename(ctx: MeetingContext, remote_id: str, *, title: str, revision: str) -> None:
    async with _editing(ctx, remote_id, revision) as current:
        async with meeting_errors(ctx):
            await ctx.meetings.rename(ctx.actor, current, title)
        saved = await read(ctx, remote_id)
    if saved.title != title:
        raise MeetingSaveUnconfirmedError("Meeting did not confirm the new title.")


async def save_minutes(ctx: MeetingContext, remote_id: str, *, content: str, revision: str) -> None:
    """Replace the minutes. Saved as the Traditional overview; the other
    languages are cleared, because they were translations of the minutes
    being replaced and would now say something else."""
    async with _editing(ctx, remote_id, revision) as current:
        _require_editable(current)
        async with meeting_errors(ctx):
            await ctx.meetings.update_recognition(
                ctx.actor, current, overviews=Overviews(tc=content, sc="", en="")
            )
        saved = await read(ctx, remote_id)
    if saved.overviews.tc != content:
        raise MeetingSaveUnconfirmedError("Meeting did not confirm the new minutes.")


async def save_transcript_line(
    ctx: MeetingContext,
    remote_id: str,
    *,
    index: int,
    content: str,
    speaker_name: str,
    revision: str,
) -> None:
    async with _editing(ctx, remote_id, revision) as current:
        _require_editable(current)
        async with meeting_errors(ctx):
            await ctx.meetings.update_transcript_line(
                ctx.actor, current, index=index, content=content, speaker_name=speaker_name
            )
        saved = await read(ctx, remote_id)
    if (
        index >= len(saved.transcript)
        or saved.transcript[index].text != content
        or saved.transcript[index].speaker != speaker_name
    ):
        raise MeetingSaveUnconfirmedError("Meeting did not confirm the transcript change.")
