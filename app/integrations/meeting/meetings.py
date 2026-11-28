"""Meeting's meeting endpoints, as calls.

Each method makes one request as the given actor and returns what it
answered as a typed record (:mod:`.records` reads it), or for the
generation endpoints the assembled text. Meeting replaces records whole, so
the writes take the record as read and send back what they do not change.
"""

from typing import Any, Literal

from app.integrations.meeting.output import read_output
from app.integrations.meeting.records import (
    LiveStream,
    MeetingListPage,
    MeetingRecord,
    Overviews,
    edit_transcript,
    read_audio_url,
    read_created_id,
    read_list,
    read_live_stream,
    read_record,
    to_wire,
)
from app.integrations.meeting.storage import UploadTicket, read_ticket
from app.integrations.meeting.transport import MeetingActor, MeetingTransport

# Meeting's list filter values.
ListFilter = Literal["all", "favorites", "trash"]
_FILTERS: dict[ListFilter, int] = {"all": 2, "favorites": 1, "trash": 0}

# How Meeting names each output language in a translation request. The
# overview endpoint wants the Traditional label in Traditional characters;
# the live-line endpoint has only been seen with the Simplified spelling,
# so each keeps the one it was observed working with.
TranslationTarget = Literal["auto", "en-US", "zh-CN", "zh-HK"]
_LINE_LANGUAGES: dict[TranslationTarget, str] = {
    "auto": "auto",
    "en-US": "英语",
    "zh-CN": "简体",
    "zh-HK": "繁体",
}
_OVERVIEW_LANGUAGES: dict[TranslationTarget, str] = {**_LINE_LANGUAGES, "zh-HK": "繁體"}

# ``reply_language`` for ``reload_overview``: 2 is Traditional Chinese, the
# language every overview this deployment generates is written in.
_OVERVIEW_REPLY_LANGUAGE = 2

PAGE_SIZE = 20


class MeetingMeetings:
    def __init__(
        self,
        transport: MeetingTransport,
        *,
        origin: str,
        overview_timeout: float,
        translate_overview_timeout: float,
    ) -> None:
        self._transport = transport
        # Meeting's own address, which a relative live-stream URL is under.
        self._origin = origin
        self._overview_timeout = overview_timeout
        self._translate_overview_timeout = translate_overview_timeout

    async def list_page(
        self, actor: MeetingActor, *, which: ListFilter, page_index: int
    ) -> MeetingListPage:
        """One page of meetings. ``page_index`` is zero-based — Meeting calls
        it ``offset`` but counts pages, not rows."""
        selected = [_FILTERS[which]]
        data = await self._transport.data(
            "POST",
            "/meeting/api/v1/list",
            actor,
            # Sent under both spellings; releases have read one or the other.
            body={
                "meetingFilters": selected,
                "meeting_filters": selected,
                "limit": PAGE_SIZE,
                "offset": page_index,
            },
        )
        return read_list(
            data,
            page_index=page_index,
            page_size=PAGE_SIZE,
            in_favorites=which == "favorites",
            in_trash=which == "trash",
        )

    async def info(self, actor: MeetingActor, meeting_id: str) -> MeetingRecord:
        data = await self._transport.data(
            "GET", "/meeting/api/v1/info", actor, params={"meeting_id": meeting_id}
        )
        return read_record(data, meeting_id)

    async def audio_url(self, actor: MeetingActor, meeting_id: str) -> str:
        """A signed link to the meeting's recording, or ``""`` if it has none."""
        data = await self._transport.data(
            "GET", "/meeting/api/v1/audio", actor, params={"meeting_id": meeting_id}
        )
        return read_audio_url(data)

    async def set_favorite(self, actor: MeetingActor, meeting_id: str, enabled: bool) -> None:
        await self._transport.data(
            "POST",
            "/meeting/api/v1/favorite",
            actor,
            body={"meeting_id": meeting_id, "favorite": enabled},
        )

    async def set_trash(self, actor: MeetingActor, meeting_id: str, enabled: bool) -> None:
        await self._transport.data(
            "POST",
            "/meeting/api/v1/trash",
            actor,
            body={"meeting_id": meeting_id, "trash": enabled},
        )

    async def delete(self, actor: MeetingActor, meeting_id: str) -> None:
        await self._transport.data(
            "DELETE", "/meeting/api/v1/meeting", actor, body={"meeting_id": meeting_id}
        )

    async def rename(self, actor: MeetingActor, record: MeetingRecord, name: str) -> None:
        await self._transport.data(
            "PUT",
            "/meeting/api/v1/meeting",
            actor,
            body={
                "meeting_id": record.meeting_id,
                "meeting_name": name,
                "remark": record.remark,
                "meeting_time_sec": record.time_sec,
            },
        )

    async def update_recognition(
        self,
        actor: MeetingActor,
        record: MeetingRecord,
        *,
        transcript: Any = None,
        overviews: Overviews | None = None,
    ) -> None:
        """Replace the transcript and/or the minutes; what is not given is
        sent back as read."""
        if overviews is None:
            overview = record.overview if record.overview is not None else record.overviews.tc
            overviews = record.overviews
        else:
            overview = overviews.tc
        await self._transport.data(
            "PUT",
            "/meeting/api/v1/info",
            actor,
            body={
                "meeting_id": record.meeting_id,
                "recognition_id": record.recognition_id,
                "recognition_message": to_wire(
                    record.raw_transcript if transcript is None else transcript
                ),
                "notes": record.notes if record.notes is not None else "",
                "overview": overview,
                "overview_tc": overviews.tc,
                "overview_sc": overviews.sc,
                "overview_en": overviews.en,
            },
        )

    async def update_transcript_line(
        self,
        actor: MeetingActor,
        record: MeetingRecord,
        *,
        index: int,
        content: str,
        speaker_name: str,
    ) -> None:
        """Replace one transcript line, keeping the transcript's own shape."""
        edited = edit_transcript(
            record.raw_transcript, index=index, content=content, speaker_name=speaker_name
        )
        await self.update_recognition(actor, record, transcript=edited)

    async def generate_overview(self, actor: MeetingActor, meeting_id: str) -> str:
        """Have Meeting write minutes from the transcript. Slow: minutes."""
        async with self._transport.stream(
            "/meeting/api/v1/reload_overview",
            actor,
            body={"meeting_id": meeting_id, "reply_language": _OVERVIEW_REPLY_LANGUAGE},
            timeout=self._overview_timeout,
        ) as response:
            return await read_output(response)

    async def translate_overview(
        self, actor: MeetingActor, *, meeting_id: str, text: str, target: TranslationTarget
    ) -> str:
        """Translate a meeting's minutes."""
        return await self._translate(
            actor,
            "/meeting/api/v1/translate_overview",
            {
                "reply_language": _OVERVIEW_LANGUAGES[target],
                "selected_content": text,
                "meeting_id": meeting_id,
            },
            timeout=self._translate_overview_timeout,
        )

    async def translate_line(
        self, actor: MeetingActor, *, text: str, target: TranslationTarget
    ) -> str:
        """Translate one utterance of a live meeting."""
        return await self._translate(
            actor,
            "/meeting/api/v1/translate",
            {
                "reply_language": _LINE_LANGUAGES[target],
                "selected_content": text,
                "meeting_id": "",
                "only_translate": "0",
            },
            timeout=self._transport.timeout,
        )

    async def _translate(
        self, actor: MeetingActor, path: str, values: dict[str, str], *, timeout: float
    ) -> str:
        parameters = {
            "word_num": "",
            "user_instruction": "",
            "uploaded_ref": "",
            "user_ref": "",
            "exist_article": "",
            "exist_article_former": "",
            "exist_article_latter": "",
            **values,
        }
        async with self._transport.stream(
            path,
            actor,
            body={
                "key": "translate_selected",
                "parameters": [{"key": key, "value": value} for key, value in parameters.items()],
                "stream": True,
            },
            timeout=timeout,
        ) as response:
            return await read_output(response)

    async def create_streaming(
        self, actor: MeetingActor, *, creator_id: str, name: str, remark: str, time_sec: int
    ) -> LiveStream:
        """Open a live-interpretation meeting and its caption stream."""
        data = await self._transport.data(
            "POST",
            "/meeting/api/v1/streaming/create_meeting",
            actor,
            body={
                "creator_id": creator_id,
                "name": name,
                "remark": remark,
                "meeting_time_sec": str(time_sec),
            },
        )
        return read_live_stream(data, self._origin)

    async def upload_ticket(self, actor: MeetingActor) -> UploadTicket:
        """Temporary object-storage credentials for uploading a recording."""
        data = await self._transport.data(
            "POST", "/meeting/api/file/v1/get_token", actor, body={"bucket": 2}
        )
        return read_ticket(data)

    async def create_meeting(
        self,
        actor: MeetingActor,
        *,
        name: str,
        remark: str,
        time_sec: int,
        file_key: str,
        file_name: str,
        duration_seconds: int,
    ) -> str:
        """File an uploaded recording as a meeting; its id. ``scene`` 3 is a
        recording made elsewhere and uploaded."""
        data = await self._transport.data(
            "POST",
            "/meeting/api/v1/meeting",
            actor,
            body={
                "meeting_name": name,
                "remark": remark,
                "meeting_time_sec": str(time_sec),
                "files": [{"file_key": file_key, "file_name": file_name}],
                "duration_seconds": duration_seconds,
                "scene": 3,
            },
        )
        return read_created_id(data)
