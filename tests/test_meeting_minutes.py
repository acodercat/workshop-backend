"""Who writes and translates minutes, and what happens when they cannot."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.errors import MeetingGenerationFailedError, MeetingNoTranscriptError
from app.models.meeting_settings import MinutesSource
from app.services.meeting import minutes
from app.services.meeting.settings import CurrentSettings
from tests.meeting_fakes import FakeMeeting, meeting_context_for


class _TranslatingMeeting(FakeMeeting):
    def __init__(self, translation: httpx.Response) -> None:
        super().__init__()
        self.translation = translation
        self.record["list"][0]["overview_en"] = ""
        self.asked = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/meeting/api/v1/translate_overview":
            self.asked = True
            return self.translation
        return super().__call__(request)


_BROKEN = httpx.Response(
    200, headers={"content-type": "text/event-stream"}, content="event: ERROR\ndata: {}\n\n"
)


def _source(monkeypatch, source: MinutesSource) -> AsyncMock:
    current = CurrentSettings(source=source, model="test-model", revision=0)
    monkeypatch.setattr(minutes.settings, "read", AsyncMock(return_value=current))
    model = AsyncMock(return_value="from the model")
    monkeypatch.setattr(minutes, "complete", model)
    return model


async def test_stored_minutes_in_the_language_win(monkeypatch) -> None:
    model = _source(monkeypatch, MinutesSource.AUTO)
    async with meeting_context_for(FakeMeeting()) as ctx:
        text = await minutes.translate(
            ctx, AsyncMock(), "m1", language="tc", source_language=None, content="ignored"
        )

    assert text == "舊紀要"
    model.assert_not_awaited()


async def test_between_chinese_scripts_is_a_conversion(monkeypatch) -> None:
    model = _source(monkeypatch, MinutesSource.AUTO)
    async with meeting_context_for(FakeMeeting()) as ctx:
        text = await minutes.translate(
            ctx, AsyncMock(), "m1", language="sc", source_language="tc", content="會議紀錄"
        )

    assert text == "会议纪录"
    model.assert_not_awaited()


async def test_auto_falls_back_to_the_model_when_meeting_cannot(monkeypatch) -> None:
    model = _source(monkeypatch, MinutesSource.AUTO)
    async with meeting_context_for(_TranslatingMeeting(_BROKEN)) as ctx:
        text = await minutes.translate(
            ctx, AsyncMock(), "m1", language="en", source_language="tc", content="會議"
        )

    assert text == "from the model"
    model.assert_awaited_once()


async def test_meeting_only_does_not_fall_back(monkeypatch) -> None:
    model = _source(monkeypatch, MinutesSource.MEETING)
    async with meeting_context_for(_TranslatingMeeting(_BROKEN)) as ctx:
        with pytest.raises(MeetingGenerationFailedError):
            await minutes.translate(
                ctx, AsyncMock(), "m1", language="en", source_language="tc", content="會議"
            )

    model.assert_not_awaited()


async def test_system_only_never_asks_meeting(monkeypatch) -> None:
    _source(monkeypatch, MinutesSource.SYSTEM)
    meeting = _TranslatingMeeting(httpx.Response(500))
    async with meeting_context_for(meeting) as ctx:
        text = await minutes.translate(
            ctx, AsyncMock(), "m1", language="en", source_language="tc", content="會議"
        )

    assert text == "from the model"
    assert not meeting.asked


async def test_minutes_already_stored_are_not_regenerated() -> None:
    async with meeting_context_for(FakeMeeting()) as ctx:
        result = await minutes.minutes(ctx, AsyncMock(), "m1", language="sc")

    # No Simplified copy is stored; the Traditional one is returned with a
    # derived Simplified variant.
    assert result.language == "tc"
    assert result.variants["sc"] == "旧纪要"


async def test_no_transcript_means_nothing_to_draft_from(monkeypatch) -> None:
    fake = FakeMeeting()
    fake.record["list"][0]["recognition_message"] = []
    async with meeting_context_for(fake) as ctx:
        with pytest.raises(MeetingNoTranscriptError):
            await minutes.draft(ctx, AsyncMock(), "m1", template=None)


async def test_system_source_writes_minutes_with_the_model(monkeypatch) -> None:
    model = _source(monkeypatch, MinutesSource.SYSTEM)
    fake = _TranslatingMeeting(httpx.Response(500))
    fake.record["list"][0]["overview_tc"] = ""
    async with meeting_context_for(fake) as ctx:
        result = await minutes.minutes(ctx, AsyncMock(), "m1", language="tc")

    assert result.content == "from the model"
    # Not stored at Meeting, so the record's revision is unchanged.
    assert fake.writes == []
    model.assert_awaited_once()


async def test_a_concurrent_writer_is_waited_for_not_duplicated(monkeypatch) -> None:
    _source(monkeypatch, MinutesSource.MEETING)
    fake = FakeMeeting()
    fake.record["list"][0]["overview_tc"] = ""
    async with meeting_context_for(fake) as ctx:
        detail = await minutes.hosted.meeting_detail(ctx, "m1")
        key = f"meeting_minutes:{ctx.user_id}:m1:{detail.remote.revision}"
        ctx.redis.values[key] = "another request"  # type: ignore[attr-defined]

        async def other_writer_finishes(_: float) -> None:
            fake.record["list"][0]["overview_tc"] = "由另一個請求寫成"
            del ctx.redis.values[key]  # type: ignore[attr-defined]

        monkeypatch.setattr(minutes.asyncio, "sleep", other_writer_finishes)
        result = await minutes.minutes(ctx, AsyncMock(), "m1", language="tc")

    assert result.content == "由另一個請求寫成"
