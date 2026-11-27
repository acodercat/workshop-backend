"""Assembling the text Meeting streams back.

Each case is a way a stream has been seen to end; what matters is which of
the three outcomes it reads as — an answer, an incomplete answer (the
caller then reads what Meeting saved), or a refusal.
"""

import httpx
import pytest

from app.integrations.meeting.output import (
    OutputEmpty,
    OutputFailed,
    OutputIncomplete,
    read_output,
)
from app.integrations.meeting.transport import MeetingDenied


def _sse(*blocks: str) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="\n\n".join(blocks) + "\n\n"
    )


async def test_appends_are_joined_and_finish_does_not_repeat_them() -> None:
    response = _sse(
        'event: APPEND\ndata: {"answer": "Hel"}',
        'event: APPEND\ndata: {"answer": "lo"}',
        # FINISH repeats the whole answer; counting it would double it.
        'event: FINISH\ndata: {"answer": "Hello"}',
    )

    assert await read_output(response) == "Hello"


async def test_openai_style_deltas_end_with_done() -> None:
    response = _sse(
        'data: {"choices": [{"delta": {"content": "a"}}]}',
        'data: {"choices": [{"delta": {"content": "b"}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    )

    assert await read_output(response) == "ab"


async def test_a_stream_that_just_stops_is_incomplete() -> None:
    with pytest.raises(OutputIncomplete):
        await read_output(_sse('event: APPEND\ndata: {"answer": "partial"}'))


async def test_a_length_cut_is_a_failure_not_an_answer() -> None:
    response = _sse('data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "length"}]}')

    with pytest.raises(OutputFailed) as caught:
        await read_output(response)

    assert caught.value.reason == "truncated"


async def test_an_error_event_carrying_an_auth_code_is_a_denial() -> None:
    with pytest.raises(MeetingDenied):
        await read_output(_sse('event: ERROR\ndata: {"code": 401}'))


async def test_a_known_error_reason_is_kept() -> None:
    with pytest.raises(OutputFailed) as caught:
        await read_output(_sse('event: ERROR\ndata: {"reason": "timeout"}'))

    assert caught.value.reason == "timeout"


async def test_a_completed_stream_with_no_text_is_empty() -> None:
    with pytest.raises(OutputEmpty):
        await read_output(_sse('event: FINISH\ndata: {"answer": ""}'))


async def test_a_single_json_answer_is_read_as_one() -> None:
    response = httpx.Response(200, json={"code": 0, "data": {"answer": "done"}})

    assert await read_output(response) == "done"
