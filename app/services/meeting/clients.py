"""Wiring the Meeting integration from settings, once per process.

Sign-in and the meeting workspace share one HTTP client and one transport:
a single connection pool to one host, and a single place the protocol's
settings are read. The web process and the background process each open
their own with :func:`open_meeting_clients`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from app.integrations.meeting.accounts import MeetingAccounts
from app.integrations.meeting.meetings import MeetingMeetings
from app.integrations.meeting.transport import MeetingTransport, open_meeting_http
from config import settings


@dataclass(frozen=True)
class MeetingClients:
    accounts: MeetingAccounts
    meetings: MeetingMeetings


@asynccontextmanager
async def open_meeting_clients(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[MeetingClients]:
    """Build the clients; close their connections on exit. ``transport``
    is for tests."""
    async with open_meeting_http(settings.meeting_service_url, transport=transport) as http:
        meeting_transport = MeetingTransport(
            http,
            app_id=settings.meeting_app_id,
            cookie_name=settings.meeting_session_cookie_name,
            accept_language=settings.meeting_accept_language,
            timeout=settings.meeting_request_timeout_seconds,
        )
        yield MeetingClients(
            accounts=MeetingAccounts(
                meeting_transport, login_public_key=settings.meeting_login_public_key
            ),
            meetings=MeetingMeetings(
                meeting_transport,
                origin=settings.meeting_service_url,
                overview_timeout=settings.meeting_overview_timeout_seconds,
                translate_overview_timeout=settings.meeting_translate_timeout_seconds,
            ),
        )
