"""The pure parts of recording: the WAV handed to Meeting, where the
browser is sent for live captions, and which files may be uploaded."""

import struct

import pytest

from app.errors import RecordingMediaUnsupportedError
from app.integrations.meeting.records import RecordInvalid, read_live_stream
from app.schemas.meeting import MediaUploadUrlRequest
from app.services.meeting.recordings import media_extension, upload_url
from app.workers.meeting_archiver import wav


def test_the_wav_header_describes_16khz_mono_pcm() -> None:
    pcm = b"\x01\x00" * 16_000  # one second, sent as two windows

    data = wav([pcm[:12_000], pcm[12_000:]])

    riff, size, wave, fmt, _, audio, channels, rate, byte_rate, align, bits, tag, length = (
        struct.unpack("<4sI4s4sIHHIIHH4sI", data[:44])
    )
    assert (riff, wave, fmt, tag) == (b"RIFF", b"WAVE", b"fmt ", b"data")
    assert (audio, channels, rate, byte_rate, align, bits) == (1, 1, 16_000, 32_000, 2, 16)
    assert size == len(data) - 8 and length == len(pcm)
    assert data[44:] == pcm


def test_a_stream_url_is_found_wherever_meeting_nests_it() -> None:
    stream = read_live_stream(
        {"data": {"data": {"ws_url": "https://live.example/ws?meeting_id=s1", "meeting_id": "m1"}}},
        "https://meeting.example",
    )

    # The socket's own id wins over the response's; https becomes wss.
    assert stream.meeting_id == "s1"
    assert stream.websocket_url == "wss://live.example/ws?meeting_id=s1"


def test_a_relative_stream_url_resolves_against_meeting() -> None:
    stream = read_live_stream({"ws_url": "/ws", "meeting_id": "m1"}, "https://meeting.example")

    assert stream.websocket_url == "wss://meeting.example/ws"


@pytest.mark.parametrize(
    "data",
    [
        {"ws_url": "ws://plain.example/ws", "meeting_id": "m1"},  # unencrypted
        {"ws_url": "wss://x/ws"},  # no meeting id
        {},
    ],
)
def test_an_unusable_stream_is_refused(data) -> None:
    with pytest.raises(RecordInvalid):
        read_live_stream(data, "https://meeting.example")


@pytest.mark.parametrize("name", ["deck.pptx", "notes.txt", "noext"])
def test_only_audio_and_video_are_accepted(name) -> None:
    with pytest.raises(RecordingMediaUnsupportedError):
        upload_url(MediaUploadUrlRequest(filename=name, size_bytes=1))


def test_extensions_are_case_insensitive() -> None:
    assert media_extension("Board.MP4") == ".mp4"
