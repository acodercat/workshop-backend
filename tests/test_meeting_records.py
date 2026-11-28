"""Reading Meeting's records, whose shape varies by endpoint and release.

The failure these guard against is silent: a transcript read with a
segment missing renumbers every later one, and an edit then lands on the
wrong line.
"""

import json

import pytest

from app.integrations.meeting.records import (
    RecordInvalid,
    TranscriptEditUnsafe,
    edit_transcript,
    parse_transcript,
    to_wire,
    validate_detail,
)


def test_a_json_string_list_is_a_transcript() -> None:
    raw = json.dumps(
        [
            {"content": "hello", "speaker": {"name": "Ann"}, "timeRange": {"start": 1500}},
            {"text": "world", "speakerName": "Bo", "startTime": "00:01:02"},
        ]
    )

    segments = parse_transcript(raw, strict=True)

    assert [(segment.speaker, segment.text, segment.start_ms) for segment in segments] == [
        ("Ann", "hello", 1500),
        ("Bo", "world", 62_000),
    ]


def test_nested_children_are_flattened_in_order() -> None:
    raw = {"paragraphs": [{"sentences": [{"text": "a"}, {"text": "b"}]}, {"text": "c"}]}

    assert [segment.text for segment in parse_transcript(raw, strict=True)] == ["a", "b", "c"]


def test_an_anonymous_speaker_is_named_by_index() -> None:
    [segment] = parse_transcript([{"content": "x", "speaker": {"displayIndex": 2}}])

    assert segment.speaker == "Speaker-2"


@pytest.mark.parametrize(
    "raw",
    [
        [{"content": "ok"}, {"unknown": 1}],  # a segment with no text in it
        [{"content": "ok"}, None],
        '[{"content": "broken"',  # unmistakably JSON, unparseable
    ],
)
def test_strict_reading_refuses_what_it_would_have_to_skip(raw) -> None:
    with pytest.raises(RecordInvalid):
        parse_transcript(raw, strict=True)


def test_a_literal_bracket_is_speech_not_json() -> None:
    [segment] = parse_transcript("[laughs] okay", strict=True)

    assert segment.text == "[laughs] okay"


def _detail(**recognition) -> dict:
    return {"code": 0, "data": {"meeting_info": {"meeting_id": "m1"}, "list": [recognition]}}


def test_a_record_for_another_meeting_is_refused() -> None:
    with pytest.raises(RecordInvalid, match="mismatch"):
        validate_detail(_detail(meeting_id="m2", recognition_message=[]), "m1")


def test_two_recognitions_are_refused_rather_than_merged() -> None:
    value = {"data": {"meeting_info": {"meeting_id": "m1"}, "list": [{}, {}]}}

    with pytest.raises(RecordInvalid, match="ambiguous"):
        validate_detail(value, "m1")


def test_an_envelope_reporting_failure_is_refused() -> None:
    with pytest.raises(RecordInvalid, match="refused"):
        validate_detail({"code": 500, "data": {}}, "m1")


def test_an_edit_changes_one_segment_and_keeps_the_shape() -> None:
    raw = json.dumps(
        [
            {"content": "one", "speaker": {"name": "A", "id": 7}, "translatedContent": "uno"},
            {"content": "two", "speaker": {"name": "B"}, "extra": "kept"},
        ]
    )

    edited = edit_transcript(raw, index=0, content="ONE", speaker_name="Ann")

    # Still a JSON string, as Meeting stored it.
    rows = json.loads(edited)
    assert rows[0]["content"] == "ONE"
    assert rows[0]["speaker"] == {"name": "Ann", "id": 7}
    # A translation of the old text would now be wrong.
    assert rows[0]["translatedContent"] == ""
    assert rows[1] == {"content": "two", "speaker": {"name": "B"}, "extra": "kept"}


def test_an_edit_past_the_end_is_refused() -> None:
    with pytest.raises(TranscriptEditUnsafe):
        edit_transcript([{"content": "only"}], index=3, content="x", speaker_name="y")


def test_keys_go_back_to_meeting_in_snake_case() -> None:
    assert to_wire([{"speakerName": "A", "timeRange": {"startMs": 1}}]) == [
        {"speaker_name": "A", "time_range": {"start_ms": 1}}
    ]
