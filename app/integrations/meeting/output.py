"""Reading the text Meeting streams back from its generation endpoints.

``reload_overview``, ``translate`` and ``translate_overview`` answer either
with one JSON object or with server-sent events. Either way the reader
assembles the text, and tells three outcomes apart that callers treat
differently:

- a complete answer — returned, trimmed;
- an incomplete or empty one — :class:`OutputIncomplete` / :class:`OutputEmpty`.
  Meeting sometimes stores a complete overview even when its stream ends
  early, so callers that can re-read the stored result do;
- a refusal — :class:`OutputFailed` with a reason, or
  :class:`~.transport.MeetingDenied` when the payload says the session is
  not signed in.

A port of the reader in Meeting's own web client, which is the only
description of this protocol there is; its quirks are kept on purpose and
called out where they appear.
"""

import json
from typing import Any

import httpx

from app.integrations.meeting.transport import (
    DENIED_CODES,
    OK_CODES,
    MeetingDenied,
    raise_for_http,
)

_REASONS: frozenset[str] = frozenset(
    {"truncated", "incomplete", "empty", "language", "unavailable", "format", "timeout"}
)
# Events whose ``data`` carries text.
_TEXT_EVENTS = frozenset({"", "message", "APPEND", "append", "FINISH", "DONE"})
_FINAL_EVENTS = frozenset({"FINISH", "DONE", "CANCEL"})


class OutputError(Exception):
    """Base for a stream that did not yield a usable answer."""


class OutputFailed(OutputError):
    """Meeting reported a failure, or sent something unreadable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OutputIncomplete(OutputError):
    """The stream ended without saying it was done, or said the answer was
    cut short for a reason other than length."""


class OutputEmpty(OutputError):
    """The stream completed with no text in it."""


def _row(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _code(value: Any) -> str | None:
    return None if value is None else str(value)


def _assert_authorized(value: Any) -> None:
    """Raise if ``value`` — or anything it nests under ``data`` / ``error`` —
    carries a not-signed-in code."""
    if not isinstance(value, dict):
        return
    if _code(value.get("code")) in DENIED_CODES:
        raise MeetingDenied
    if value.get("data") is not None and value.get("data") is not value:
        _assert_authorized(value["data"])
    if isinstance(value.get("error"), dict):
        _assert_authorized(value["error"])


class _Assembler:
    def __init__(self) -> None:
        self.text = ""
        self.completed = False

    def consume(self, value: Any, *, final: bool = False) -> None:
        if not isinstance(value, dict):
            return
        _assert_authorized(value)
        for layer in (value, _row(value.get("data")) or value):
            code = _code(layer.get("code"))
            if layer.get("error") or (code is not None and code not in OK_CODES):
                raise OutputFailed("unavailable")
        payload = _row(value.get("data")) or value
        choices = payload.get("choices")
        choice = _row(choices[0]) if isinstance(choices, list) and choices else {}
        for reason in (
            choice.get("finish_reason"),
            payload.get("finish_reason"),
            value.get("finish_reason"),
        ):
            if reason in ("length", "max-tokens"):
                raise OutputFailed("truncated")
            if reason is not None and reason != "stop":
                raise OutputIncomplete
        delta = _row(choice.get("delta"))
        message_event = _row(delta.get("message_event"))
        chunk = _first_present(
            payload.get("answer"),
            delta.get("content"),
            _row(choice.get("message")).get("content"),
            message_event.get("answer") if message_event.get("event") == "overview" else None,
            payload.get("text"),
            payload.get("content"),
        )
        # FINISH repeats the complete answer after the APPEND events, so it
        # only counts when nothing came before it.
        if isinstance(chunk, str) and chunk and (not final or not self.text):
            self.text += chunk

    def consume_block(self, block: str) -> None:
        lines = block.splitlines()
        event = next((line[6:].strip() for line in lines if line.startswith("event:")), "")
        raw = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        if event == "FALLBACK":
            return
        if event.upper() == "ERROR":
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            _assert_authorized(value)
            reason = _row(value).get("reason")
            raise OutputFailed(
                reason if isinstance(reason, str) and reason in _REASONS else "unavailable"
            )
        if event.upper() == "CANCEL":
            raise OutputFailed("cancelled")
        final = event in _FINAL_EVENTS
        if final or raw == "[DONE]":
            self.completed = True
        if not raw or raw == "[DONE]":
            return
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise OutputFailed("format") from exc
        if event in _TEXT_EVENTS:
            self.consume(value, final=final)


def _first_present(*values: Any) -> Any:
    """JavaScript's ``??`` chain: the first value that is not ``None``."""
    return next((value for value in values if value is not None), None)


async def read_output(response: httpx.Response) -> str:
    """Read a generation response to the end; return its text, trimmed."""
    raise_for_http(response)
    assembler = _Assembler()
    if "application/json" in response.headers.get("content-type", ""):
        await response.aread()
        try:
            value = response.json()
        except ValueError as exc:
            raise OutputFailed("format") from exc
        assembler.consume(value)
    else:
        pending = ""
        async for piece in response.aiter_text():
            pending += piece.replace("\r\n", "\n")
            *blocks, pending = pending.split("\n\n")
            for block in blocks:
                assembler.consume_block(block)
                if assembler.completed:
                    break
            if assembler.completed:
                break
        else:
            if pending.strip():
                assembler.consume_block(pending)
        if not assembler.completed:
            raise OutputIncomplete
    text = assembler.text.strip()
    if not text:
        raise OutputEmpty
    return text
