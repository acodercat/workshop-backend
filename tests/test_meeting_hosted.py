"""Hosted meetings, driven through a fake Meeting.

The fake keeps one record and answers the endpoints the workspace uses,
so an edit is exercised the way it runs: read, compare revisions, write,
read back.
"""

import uuid

import httpx
import pytest

from app.errors import (
    MeetingChangedError,
    MeetingRecordUnreadableError,
    MeetingSaveUnconfirmedError,
    MeetingSessionExpiredError,
)
from app.integrations.meeting.meetings import MeetingMeetings
from app.integrations.meeting.transport import (
    MeetingActor,
    MeetingSession,
    MeetingTransport,
    open_meeting_http,
)
from app.services.meeting import context as meeting_context, hosted
from tests.meeting_fakes import FakeMeeting, meeting_context_for


async def test_detail_reads_transcript_and_minutes() -> None:
    async with meeting_context_for(FakeMeeting()) as ctx:
        detail = await hosted.meeting_detail(ctx, "m1")

    assert detail.id == "meeting-m1"
    assert detail.title == "Board"
    assert detail.summary == "舊紀要"
    assert [(line.speaker, line.text) for line in detail.transcript] == [("A", "hi")]
    assert detail.remote is not None and detail.remote.editable


async def test_minutes_saved_at_the_read_revision_land() -> None:
    fake = FakeMeeting()
    async with meeting_context_for(fake) as ctx:
        before = await hosted.meeting_detail(ctx, "m1")
        assert before.remote is not None
        await hosted.save_minutes(ctx, "m1", content="新紀要", revision=before.remote.revision)
        after = await hosted.meeting_detail(ctx, "m1")

    assert after.summary == "新紀要"
    # The other languages were translations of the old minutes.
    [(_, body)] = fake.writes
    assert body["overview_en"] == "" and body["overview_sc"] == ""


async def test_an_edit_against_a_stale_revision_is_refused() -> None:
    fake = FakeMeeting()
    async with meeting_context_for(fake) as ctx:
        with pytest.raises(MeetingChangedError):
            await hosted.save_minutes(ctx, "m1", content="x", revision="stale")

    assert fake.writes == []


async def test_a_concurrent_edit_of_the_same_meeting_is_refused() -> None:
    fake = FakeMeeting()
    async with meeting_context_for(fake) as ctx:
        revision = (await hosted.meeting_detail(ctx, "m1")).remote.revision
        ctx.redis.values[f"meeting_edit:{ctx.user_id}:m1"] = "someone-else"  # type: ignore[attr-defined]
        with pytest.raises(MeetingChangedError):
            await hosted.rename(ctx, "m1", title="New", revision=revision)

    assert fake.writes == []


async def test_a_write_meeting_does_not_show_is_unconfirmed() -> None:
    fake = FakeMeeting()
    fake.drop_writes = True
    async with meeting_context_for(fake) as ctx:
        revision = (await hosted.meeting_detail(ctx, "m1")).remote.revision
        with pytest.raises(MeetingSaveUnconfirmedError):
            await hosted.rename(ctx, "m1", title="New", revision=revision)
        # The lock is released either way.
        assert ctx.redis.values == {}  # type: ignore[attr-defined]


async def test_a_transcript_edit_lands_on_its_line() -> None:
    fake = FakeMeeting()
    async with meeting_context_for(fake) as ctx:
        revision = (await hosted.meeting_detail(ctx, "m1")).remote.revision
        await hosted.save_transcript_line(
            ctx, "m1", index=0, content="hello", speaker_name="Ann", revision=revision
        )
        detail = await hosted.meeting_detail(ctx, "m1")

    assert [(line.speaker, line.text) for line in detail.transcript] == [("Ann", "hello")]


async def test_meeting_denying_the_session_ends_it_here(monkeypatch) -> None:
    ended: list[uuid.UUID] = []

    async def end_refused_session(session_id, *, refused):
        ended.append(session_id)
        return True

    monkeypatch.setattr(meeting_context, "end_refused_session", end_refused_session)
    fake = FakeMeeting()
    fake.deny = True
    async with meeting_context_for(fake) as ctx:
        with pytest.raises(MeetingSessionExpiredError):
            await hosted.meeting_detail(ctx, "m1")

    assert ended == [ctx.session_id]


async def test_a_page_past_the_end_answers_with_the_last_page() -> None:
    fake = FakeMeeting()
    fake.list_pages = {
        0: {"total": 1, "meeting_infos": [{"meeting_id": "a", "recognition_status": "1"}]},
        4: {"total": 1, "meeting_infos": []},
    }
    async with meeting_context_for(fake) as ctx:
        page = await hosted.list_meetings(ctx, view="meetings", page=5)

    assert page.page == 1
    assert [(item.id, item.status) for item in page.items] == [("meeting-a", "complete")]


async def test_flags_default_to_the_list_they_came_from() -> None:
    fake = FakeMeeting()
    fake.list_pages = {0: {"total": 1, "meeting_infos": [{"meeting_id": "a"}]}}
    async with meeting_context_for(fake) as ctx:
        page = await hosted.list_meetings(ctx, view="recycleBin", page=1)

    assert page.items[0].trashed and not page.items[0].favorite


async def test_an_unreadable_list_is_not_shown_as_empty() -> None:
    fake = FakeMeeting()
    fake.list_pages = {0: {"total": 3, "meeting_infos": []}}
    async with meeting_context_for(fake) as ctx:
        with pytest.raises(MeetingRecordUnreadableError):
            await hosted.list_meetings(ctx, view="meetings", page=1)


async def test_recycle_bin_deletion_needs_every_id_still_in_the_bin() -> None:
    fake = FakeMeeting()
    fake.list_pages = {0: {"total": 1, "meeting_infos": [{"meeting_id": "a", "trash": 1}]}}
    async with meeting_context_for(fake) as ctx:
        with pytest.raises(MeetingChangedError):
            await hosted.delete_from_recycle_bin(ctx, ids=["meeting-a", "meeting-b"], page=1)
        assert fake.deleted == []
        assert await hosted.delete_from_recycle_bin(ctx, ids=["meeting-a"], page=1) == 1

    assert fake.deleted == ["a"]


async def test_a_rotated_passport_is_handed_to_the_owner() -> None:
    rotations: list[tuple[str, str]] = []

    async def on_rotate(old, new):
        rotations.append((old.passport, new.passport))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "data": FakeMeeting().record},
            headers={"set-cookie": "passport=fresh; Path=/"},
        )

    async with open_meeting_http(
        "https://meeting.test", transport=httpx.MockTransport(handler)
    ) as http:
        transport = MeetingTransport(
            http, app_id="2", cookie_name="passport", accept_language="zh-HK", timeout=5
        )
        actor = MeetingActor(MeetingSession("stale", "d"), on_rotate=on_rotate)
        meetings = MeetingMeetings(
            transport,
            origin="https://meeting.test",
            overview_timeout=5,
            translate_overview_timeout=5,
        )
        await meetings.info(actor, "m1")

    assert rotations == [("stale", "fresh")]
    assert actor.session.passport == "fresh"
