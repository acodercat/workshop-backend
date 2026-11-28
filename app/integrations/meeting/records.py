"""Reading Meeting's answers, whose shape is not fixed, into typed records.

Meeting has answered with the same information under different field
names (snake and camel case, ``summary`` vs ``overview``), nested at
different depths, with the transcript as a list, a JSON string of a list,
or a tree of ``items`` / ``segments`` / ``sentences``. Every answer
:class:`~.meetings.MeetingMeetings` returns is read here, which looks each
field up under every spelling on record and fails loudly —
:class:`RecordInvalid` — on a shape it cannot read rather than showing a
meeting with parts missing. Nothing past this module sees Meeting's
spellings.

The transcript and detail rules are a port of the reader in Meeting's own
web client, kept as they are because they are the only description of the
format there is.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app.integrations.meeting.transport import OK_CODES, MeetingError

Row = dict[str, Any]

_TRANSCRIPT_KEYS = (
    "recognitionMessage",
    "recognition_message",
    "transcript",
    "transcriptText",
    "transcript_text",
    "segments",
    "sentences",
    "paragraphs",
    "records",
)
_CONTENT_KEYS = ("content", "text", "message", "sentence", "transcriptText", "transcript_text")
_TRANSLATION_KEYS = ("translatedContent", "translated_content")
_CHILD_KEYS = ("items", "records", "segments", "sentences", "paragraphs", "list", "data")
_SPEAKER_KEYS = ("speakerName", "speaker_name", "speaker", "role", "userName", "user_name")
_RECOGNITION_MARKERS = (
    "recognition_id",
    "recognitionId",
    "recognition_message",
    "recognitionMessage",
)
_MAX_DEPTH = 12


class RecordInvalid(MeetingError):
    """An answer this module cannot read without guessing.

    ``reason`` is a stable code for logs. A shape that would make a
    transcript lose or merge segments counts: showing a partial transcript
    as if it were whole is worse than refusing.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def row_of(value: Any) -> Row:
    return value if isinstance(value, dict) else {}


def _scalar_text(value: Any) -> str | None:
    """A string or number as JavaScript would print it, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(int(value)) if value.is_integer() else repr(value)
    return None


def first_text(row: Row, *keys: str) -> str:
    """The first of ``keys`` holding non-blank text, trimmed; else ``""``."""
    for key in keys:
        text = _scalar_text(row.get(key))
        if text is not None and text.strip():
            return text.strip()
    return ""


def _first_raw_text(row: Row, *keys: str) -> str:
    """As :func:`first_text`, but the value is returned untrimmed — the
    meeting list keeps titles and summaries exactly as Meeting sent them."""
    for key in keys:
        text = _scalar_text(row.get(key))
        if text is not None and text.strip():
            return text
    return ""


def _array_value(value: Any) -> list[Any]:
    """A list, or a JSON string of one; anything else reads as empty."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return parsed
    return []


def _detail_payload(value: Any) -> Row:
    current = row_of(value)
    for _ in range(5):
        nested = row_of(current.get("data"))
        if not nested:
            break
        current = nested
    return current


@dataclass(frozen=True)
class _RecordParts:
    payload: Row
    rows: list[Any]
    recognition: Row
    info: Row
    container: Any


def _record_parts(value: Any) -> _RecordParts:
    """Split a detail answer into its meeting info and its one recognition."""
    payload = _detail_payload(value)
    records = _array_value(payload.get("records"))
    records_are_recognition = any(
        any(key in row_of(item) for key in _RECOGNITION_MARKERS) for item in records
    )
    container = coalesce(
        payload.get("list"),
        payload.get("items"),
        payload.get("records") if records_are_recognition else None,
    )
    rows = _array_value(container)
    recognition = row_of(rows[0]) if rows else payload
    info = row_of(coalesce(payload.get("meetingInfo"), payload.get("meeting_info")))
    return _RecordParts(payload, rows, recognition, info, container)


def coalesce(*values: Any) -> Any:
    """JavaScript's ``??`` chain: the first value that is not ``None``."""
    return next((value for value in values if value is not None), None)


def _transcript_value(row: Row) -> Any:
    """The transcript field of a recognition, preferring a non-empty one."""
    candidates = [row.get(key) for key in _TRANSCRIPT_KEYS]
    for value in candidates:
        if value is not None and value != "" and value != [] and value != "[]":
            return value
    return next((value for value in candidates if value is not None), None)


@dataclass(frozen=True)
class Segment:
    speaker: str
    text: str
    translation: str
    start_ms: int


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _clock_ms(value: str) -> float:
    """``"hh:mm:ss"`` (or plain milliseconds) to milliseconds."""
    if ":" in value:
        total = 0.0
        for part in value.split(":"):
            total = total * 60 + _finite(part)
        return total * 1000
    return _finite(value)


def parse_transcript(value: Any, *, strict: bool = False, depth: int = 0) -> list[Segment]:
    """Flatten a transcript, in order, into segments.

    ``strict`` refuses anything that cannot be read as segments instead of
    skipping it — used wherever the result is shown or edited, because a
    skipped segment would silently renumber everything after it.
    """
    if depth > _MAX_DEPTH:
        raise RecordInvalid("transcript-depth")
    if value is None or value == "":
        return []
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            # A literal bracket may simply begin what was said — unless it
            # unmistakably opens a JSON list that failed to parse.
            if strict and re.match(r'^\[\s*[\[{"]', value.strip()):
                raise RecordInvalid("transcript-json") from None
        else:
            if isinstance(parsed, list):
                return parse_transcript(parsed, strict=strict, depth=depth + 1)
    if isinstance(value, list):
        segments: list[Segment] = []
        for item in value:
            if strict and (item is None or isinstance(item, (bool, int, float))):
                raise RecordInvalid("transcript-item")
            segments.extend(parse_transcript(item, strict=strict, depth=depth + 1))
        return segments

    row = {"content": value} if isinstance(value, str) else row_of(value)
    content = first_text(row, *_CONTENT_KEYS)
    translation = first_text(row, *_TRANSLATION_KEYS)
    if not content and not translation:
        for key in _CHILD_KEYS:
            if key in row:
                return parse_transcript(row[key], strict=strict, depth=depth + 1)
        if strict:
            # A segment with no text in it — an object without a text
            # field, a blank string, a bare number — is one we cannot read.
            raise RecordInvalid("transcript-segment")
        return []

    speaker = row_of(row.get("speaker"))
    time_range = row_of(coalesce(row.get("timeRange"), row.get("time_range")))
    nested_start = first_text(time_range, "start")
    raw_time = first_text(row, "startTime", "start_time", "timestamp", "time")
    start_ms = _finite(nested_start) if nested_start else _clock_ms(raw_time)
    display_index = first_text(speaker, "displayIndex", "display_index")
    speaker_name = (
        first_text(speaker, "name", "speakerName", "speaker_name")
        or first_text(row, *_SPEAKER_KEYS)
        or (f"Speaker-{display_index}" if display_index else "")
    )
    return [
        Segment(
            speaker=speaker_name,
            text=content,
            translation=translation,
            start_ms=int(start_ms),
        )
    ]


_SUMMARY_KEYS = (
    "overviewTc",
    "overview_tc",
    "summaryTc",
    "summary_tc",
    "overviewSc",
    "overview_sc",
    "summarySc",
    "summary_sc",
    "overviewEn",
    "overview_en",
    "summaryEn",
    "summary_en",
    "overview",
    "summary",
    "content",
)


def validate_detail(value: Any, expected_id: str) -> _RecordParts:
    """Check a detail answer is one readable record for ``expected_id``.

    Refuses rather than repairs: an envelope reporting failure, more than
    one recognition (which one would the minutes be about?), a record for
    a different meeting, or a transcript :func:`parse_transcript` cannot
    read strictly.
    """
    envelope = row_of(value)
    if not envelope:
        raise RecordInvalid("detail-empty")
    for _ in range(5):
        code = envelope.get("code")
        if (
            (code is not None and str(code) not in OK_CODES)
            or envelope.get("success") is False
            or envelope.get("ok") is False
        ):
            raise RecordInvalid("detail-refused")
        nested = row_of(envelope.get("data"))
        if not nested:
            break
        envelope = nested

    parts = _record_parts(value)
    container = parts.container
    if (
        container is not None
        and not isinstance(container, list)
        and not (isinstance(container, str) and _array_value(container))
    ):
        raise RecordInvalid("detail-container")
    if len(parts.rows) > 1:
        raise RecordInvalid("detail-ambiguous")
    ids = [
        found
        for found in (
            first_text(parts.info, "meetingId", "meeting_id", "id"),
            first_text(parts.recognition, "meetingId", "meeting_id"),
            first_text(parts.payload, "meetingId", "meeting_id"),
        )
        if found
    ]
    if any(found != expected_id for found in ids):
        raise RecordInvalid("detail-mismatch")
    merged = {**parts.recognition, **parts.info}
    if (
        not ids
        and not first_text(merged, "meetingName", "meeting_name", "title", "name")
        and _transcript_value(merged) is None
        and not first_text(merged, *_SUMMARY_KEYS)
    ):
        raise RecordInvalid("detail-unrecognized")
    parse_transcript(_transcript_value(parts.recognition), strict=True)
    return parts


class TranscriptEditUnsafe(MeetingError):
    """The edit could not be applied without changing more than one segment."""


def edit_transcript(value: Any, *, index: int, content: str, speaker_name: str) -> Any:
    """Return ``value`` with segment ``index`` replaced, in its own shape.

    Patches the original structure rather than rebuilding it from the
    parsed segments: Meeting stores fields this module does not read, and
    a rebuild would drop them. The result must parse to as many segments
    as the input — anything else means the edit landed somewhere it should
    not have, and is refused.
    """
    position = 0
    changed = False

    def visit(source: Any, depth: int) -> Any:
        nonlocal position, changed
        if depth > _MAX_DEPTH:
            raise TranscriptEditUnsafe
        if isinstance(source, str) and source.strip().startswith("["):
            try:
                decoded = json.loads(source)
            except ValueError:
                decoded = None  # a literal bracket may be part of the speech
            if isinstance(decoded, list):
                return json.dumps(
                    visit(decoded, depth + 1), ensure_ascii=False, separators=(",", ":")
                )
        if isinstance(source, list):
            return [visit(item, depth + 1) for item in source]
        row = row_of(source)
        is_text = isinstance(source, str) and bool(source.strip())
        content_key = next(
            (candidate for candidate in _CONTENT_KEYS if first_text(row, candidate)), None
        )
        if is_text or content_key or first_text(row, *_TRANSLATION_KEYS):
            current = position
            position += 1
            if current != index:
                return source
            changed = True
            if is_text:
                return {"content": content, "speaker_name": speaker_name}
            patched = {**row, content_key or "content": content}
            speaker = row_of(row.get("speaker"))
            if speaker:
                patched["speaker"] = {**speaker, "name": speaker_name}
            else:
                speaker_key = next(
                    (candidate for candidate in _SPEAKER_KEYS if first_text(row, candidate)),
                    "speaker_name",
                )
                patched[speaker_key] = speaker_name
            # A translation of the old text must not be shown as a
            # translation of the correction.
            if content != first_text(row, *_CONTENT_KEYS):
                for translation_key in _TRANSLATION_KEYS:
                    if translation_key in patched:
                        patched[translation_key] = ""
            return patched
        for child in _CHILD_KEYS:
            if child in row:
                return {**row, child: visit(row[child], depth + 1)}
        return source

    result = visit(value, 0)
    try:
        same_length = len(parse_transcript(value, strict=True)) == len(
            parse_transcript(result, strict=True)
        )
    except RecordInvalid as exc:
        raise TranscriptEditUnsafe from exc
    if not changed or not same_length:
        raise TranscriptEditUnsafe
    return result


_UPPER = re.compile(r"[A-Z]")


def to_wire(value: Any) -> Any:
    """camelCase keys to snake_case, recursively — the spelling Meeting
    accepts on write, whichever it used on read."""
    if isinstance(value, list):
        return [to_wire(item) for item in value]
    if isinstance(value, dict):
        return {
            _UPPER.sub(lambda match: "_" + match.group(0).lower(), key): to_wire(nested)
            for key, nested in value.items()
        }
    return value


# Typed records: what the rest of the application reads.

MeetingStatus = Literal["processing", "complete", "failed"]


@dataclass(frozen=True)
class Overviews:
    """Minutes (or summaries) in Traditional, Simplified and English;
    ``""`` where there are none."""

    tc: str
    sc: str
    en: str


@dataclass(frozen=True)
class MeetingListing:
    """A meeting as the list shows it."""

    meeting_id: str
    title: str
    time: datetime | None
    summary: str
    summaries: Overviews
    status: MeetingStatus
    favorite: bool
    trashed: bool


@dataclass(frozen=True)
class MeetingListPage:
    items: list[MeetingListing]
    total: int


@dataclass(frozen=True)
class MeetingRecord(MeetingListing):
    """One meeting, read whole: what it shows, a fingerprint of it, and what
    writing it back has to send unchanged."""

    transcript: list[Segment]
    overviews: Overviews
    # Empty until Meeting has a recognition to write edits and minutes to.
    recognition_id: str
    # Changes whenever any field an edit can change does.
    revision: str
    # As read, for writing back: Meeting replaces records whole.
    raw_transcript: Any
    notes: Any
    overview: Any
    remark: Any
    time_sec: Any

    @property
    def editable(self) -> bool:
        return bool(self.recognition_id)


@dataclass(frozen=True)
class LiveStream:
    """Where the browser connects for a live meeting's captions."""

    meeting_id: str
    websocket_url: str


def _enabled(value: Any) -> bool:
    return value is True or value == 1 or value == "1"


def _time(row: Row) -> datetime | None:
    seconds = _first_raw_text(row, "meeting_time_sec", "meetingTimeSec")
    if seconds:
        try:
            return datetime.fromtimestamp(float(seconds), UTC)
        except (ValueError, OverflowError, OSError):
            pass
    text = first_text(row, "meeting_time", "meetingTime", "created_at", "createdAt")
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _listing_fields(row: Row, *, in_favorites: bool, in_trash: bool) -> dict[str, Any]:
    meeting_id = _first_raw_text(row, "meeting_id", "meetingId", "id", "source_id", "sourceId")
    if not meeting_id:
        raise RecordInvalid("list-item-id")
    description = _first_raw_text(
        row,
        "overview_tc",
        "overviewTc",
        "overview",
        "summary_tc",
        "summaryTc",
        "summary_sc",
        "summarySc",
        "summary_en",
        "summaryEn",
        "summary",
    )
    recognition = _first_raw_text(row, "recognition_status", "recognitionStatus")
    status: MeetingStatus
    if recognition == "1" or (not recognition and description):
        status = "complete"
    elif recognition in ("0", "2"):
        status = "failed"
    else:
        status = "processing"
    return {
        "meeting_id": meeting_id,
        "title": _first_raw_text(row, "meeting_name", "meetingName", "title"),
        "time": _time(row),
        "summary": description,
        "summaries": Overviews(
            tc=_first_raw_text(row, "summary_tc", "summaryTc"),
            sc=_first_raw_text(row, "summary_sc", "summarySc"),
            en=_first_raw_text(row, "summary_en", "summaryEn"),
        ),
        "status": status,
        # When Meeting omits a flag, the list the meeting came from implies it.
        "favorite": _enabled(
            coalesce(
                row.get("is_favorite"), row.get("isFavorite"), row.get("favorite"), in_favorites
            )
        ),
        "trashed": _enabled(
            coalesce(row.get("trash"), row.get("isTrash"), row.get("is_trash"), in_trash)
        ),
    }


def read_list(
    data: Any, *, page_index: int, page_size: int, in_favorites: bool, in_trash: bool
) -> MeetingListPage:
    payload = row_of(data)
    records = coalesce(
        payload.get("meeting_infos"),
        payload.get("meetingInfos"),
        payload.get("meeting_summary_infos"),
        payload.get("meetingSummaryInfos"),
        payload.get("list"),
        payload.get("items"),
        payload.get("records"),
        [],
    )
    total = payload.get("total", 0)
    # Meeting has sent the count both as a number and as a numeric string.
    if isinstance(total, str) and total.isdigit():
        total = int(total)
    if (
        not isinstance(records, list)
        or len(records) > page_size
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < len(records)
        or (page_index == 0 and total > 0 and not records)
    ):
        raise RecordInvalid("list")
    return MeetingListPage(
        items=[
            MeetingListing(
                **_listing_fields(row_of(item), in_favorites=in_favorites, in_trash=in_trash)
            )
            for item in records
        ],
        total=total,
    )


def read_record(data: Any, meeting_id: str) -> MeetingRecord:
    parts = validate_detail(data, meeting_id)
    recognition = parts.recognition
    info = parts.info
    raw = _transcript_value(recognition)
    overviews = Overviews(
        tc=_first_raw_text(
            recognition,
            "overviewTc",
            "overview_tc",
            "summaryTc",
            "summary_tc",
            "overview",
            "summary",
            "content",
        ),
        sc=_first_raw_text(recognition, "overviewSc", "overview_sc", "summarySc", "summary_sc"),
        en=_first_raw_text(recognition, "overviewEn", "overview_en", "summaryEn", "summary_en"),
    )
    recognition_id = _first_raw_text(recognition, "recognition_id", "recognitionId")
    title = _first_raw_text(info, "meeting_name", "meetingName")
    time_sec = coalesce(info.get("meeting_time_sec"), info.get("meetingTimeSec"))
    fingerprint = json.dumps(
        [
            meeting_id,
            raw,
            [overviews.tc, overviews.sc, overviews.en],
            recognition.get("notes"),
            recognition.get("overview"),
            title,
            info.get("remark"),
            _first_raw_text(info, "meeting_time_sec", "meetingTimeSec"),
            recognition_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    listing = _listing_fields(
        {**recognition, **info, "meeting_id": meeting_id}, in_favorites=False, in_trash=False
    )
    return MeetingRecord(
        **listing,
        transcript=parse_transcript(raw, strict=True),
        overviews=overviews,
        recognition_id=recognition_id,
        revision=hashlib.sha256(fingerprint.encode()).hexdigest(),
        raw_transcript=raw,
        notes=recognition.get("notes"),
        overview=recognition.get("overview"),
        remark=info.get("remark"),
        time_sec=time_sec,
    )


def read_audio_url(data: Any) -> str:
    """The recording's signed link, or ``""`` if there is none. Only an
    https link counts: the browser plays from it directly."""
    payload = row_of(data)
    file = row_of(
        next(
            iter(
                _array_value(
                    coalesce(
                        payload.get("signedFile"), payload.get("signed_file"), payload.get("files")
                    )
                )
            ),
            None,
        )
    )
    url = first_text(file, "signedUrl", "signed_url", "url")
    return url if url.lower().startswith("https://") else ""


def _nested_text(value: Any, *keys: str) -> str:
    current = row_of(value)
    while current:
        found = first_text(current, *keys)
        if found:
            return found
        current = row_of(current.get("data"))
    return ""


def read_live_stream(data: Any, origin: str) -> LiveStream:
    """The caption stream a live meeting opened. Only ever an encrypted
    socket: the stream carries the meeting's audio."""
    raw_url = _nested_text(data, "ws_url", "websocketUrl", "websocket_url")
    meeting_id = _nested_text(data, "meeting_id", "meetingId")
    if not raw_url or not meeting_id:
        raise RecordInvalid("live-stream")
    parts = urlsplit(raw_url if "://" in raw_url else origin + "/" + raw_url.lstrip("/"))
    scheme = {"https": "wss", "wss": "wss"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise RecordInvalid("live-stream-url")
    url = urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))
    stream_id = parse_qs(parts.query).get("meeting_id", [meeting_id])[0]
    return LiveStream(meeting_id=stream_id, websocket_url=url)


def read_created_id(data: Any) -> str:
    """The id of a meeting just created."""
    if isinstance(data, dict):
        meeting_id = first_text(data, "meeting_id", "meetingId", "id")
    elif isinstance(data, (str, int)) and not isinstance(data, bool):
        meeting_id = str(data).strip()
    else:
        meeting_id = ""
    if not meeting_id:
        raise RecordInvalid("created-id")
    return meeting_id
