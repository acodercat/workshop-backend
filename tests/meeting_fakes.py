"""A fake Meeting, and a :class:`MeetingContext` driving it, for the
meeting-workspace tests."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.integrations.meeting.meetings import MeetingMeetings
from app.integrations.meeting.transport import (
    MeetingActor,
    MeetingSession,
    MeetingTransport,
    open_meeting_http,
)
from app.services.meeting.context import MeetingContext


class FakeLock:
    """The part of redis-py's ``Lock`` that ``short_lock`` uses."""

    def __init__(self, values: dict[str, str], key: str) -> None:
        self._values = values
        self._key = key
        self._token = str(uuid.uuid4())

    async def acquire(self) -> bool:
        if self._key in self._values:
            return False
        self._values[self._key] = self._token
        return True

    async def release(self) -> None:
        if self._values.get(self._key) == self._token:
            del self._values[self._key]

    async def locked(self) -> bool:
        return self._key in self._values


class FakeRedis:
    def __init__(self) -> None:
        # Held locks, by key.
        self.values: dict[str, str] = {}

    def lock(self, key, timeout=None, blocking=True):
        return FakeLock(self.values, key)


class FakeMeeting:
    """One meeting, ``m1``, plus whatever list pages a test sets."""

    def __init__(self) -> None:
        self.record = {
            "meeting_info": {"meeting_id": "m1", "meeting_name": "Board", "remark": "r"},
            "list": [
                {
                    "recognition_id": "r1",
                    "overview_tc": "舊紀要",
                    "recognition_message": [{"content": "hi", "speaker": {"name": "A"}}],
                }
            ],
        }
        self.list_pages: dict[int, dict] = {}
        self.writes: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        # When set, PUTs are accepted but not applied — a write Meeting
        # acknowledges and then does not show.
        self.drop_writes = False
        self.deny = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.deny:
            return httpx.Response(200, json={"code": 401})
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/meeting/api/v1/info" and request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": self.record})
        if path == "/meeting/api/v1/info" and request.method == "PUT":
            self.writes.append((path, body))
            if not self.drop_writes:
                self.record["list"][0]["overview_tc"] = body["overview_tc"]
                self.record["list"][0]["recognition_message"] = body["recognition_message"]
            return httpx.Response(200, json={"code": 0})
        if path == "/meeting/api/v1/meeting" and request.method == "PUT":
            self.writes.append((path, body))
            if not self.drop_writes:
                self.record["meeting_info"]["meeting_name"] = body["meeting_name"]
            return httpx.Response(200, json={"code": 0})
        if path == "/meeting/api/v1/meeting" and request.method == "DELETE":
            self.deleted.append(body["meeting_id"])
            return httpx.Response(200, json={"code": 0})
        if path == "/meeting/api/v1/list":
            return httpx.Response(
                200, json={"code": 0, "data": self.list_pages.get(body["offset"], {"total": 0})}
            )
        return httpx.Response(404)


@asynccontextmanager
async def meeting_context_for(fake: FakeMeeting) -> AsyncIterator[MeetingContext]:
    async with open_meeting_http(
        "https://meeting.test", transport=httpx.MockTransport(fake)
    ) as http:
        transport = MeetingTransport(
            http, app_id="2", cookie_name="passport", accept_language="zh-HK", timeout=5
        )
        yield MeetingContext(
            meetings=MeetingMeetings(
                transport,
                origin="https://meeting.test",
                overview_timeout=5,
                translate_overview_timeout=5,
            ),
            actor=MeetingActor(MeetingSession("p", "d")),
            account_id="42",
            session_id=uuid.uuid4(),
            user_id=1,
            redis=FakeRedis(),  # type: ignore[arg-type]
        )
