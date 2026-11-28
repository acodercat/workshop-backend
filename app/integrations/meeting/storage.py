"""Putting a file into Meeting's object storage.

Meeting does not take recordings in the request that files them: it hands
out short-lived credentials for its Huawei OBS bucket (``get_token``), the
file goes there, and the meeting is then created naming the object's key.

The object key is content-addressed (MD5 of the bytes, as Meeting's own
client does), so a retry after a failure part-way overwrites the same
object instead of leaving a second one behind.
"""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from obs import ObsClient

from app.integrations.meeting.records import first_text, row_of
from app.integrations.meeting.transport import MeetingUnavailable

_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class UploadTicket:
    """Temporary credentials for one upload into Meeting's bucket."""

    server: str
    bucket: str
    key_prefix: str
    access_key: str
    secret_key: str
    security_token: str


def read_ticket(data: Any) -> UploadTicket:
    ticket_row = row_of(data)
    endpoint = first_text(ticket_row, "endpoint")
    server = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint)
    ticket = UploadTicket(
        server=f"https://{server.netloc}",
        bucket=first_text(ticket_row, "bucket"),
        key_prefix=first_text(ticket_row, "keyPrefix", "key_prefix"),
        access_key=first_text(ticket_row, "accessKey", "access_key"),
        secret_key=first_text(ticket_row, "secretKey", "secret_key"),
        security_token=first_text(ticket_row, "securityToken", "security_token"),
    )
    if (
        server.scheme != "https"
        or not server.netloc
        or not (ticket.access_key and ticket.secret_key and ticket.security_token and ticket.bucket)
    ):
        raise MeetingUnavailable("Meeting handed out an unusable upload ticket")
    return ticket


def _put(ticket: UploadTicket, key: str, content: bytes) -> None:
    client = ObsClient(
        access_key_id=ticket.access_key,
        secret_access_key=ticket.secret_key,
        security_token=ticket.security_token,
        server=ticket.server,
        # The SDK's defaults are the unsafe ones: certificate checks off and
        # redirects followed. Temporary credentials go in every request.
        ssl_verify=True,
        max_redirect_count=0,
        max_retry_count=2,
        timeout=_TIMEOUT_SECONDS,
    )
    try:
        response = client.putContent(ticket.bucket, key, content=content)
    except Exception as exc:  # the SDK raises its own and the transport's
        raise MeetingUnavailable(f"upload failed: {type(exc).__name__}") from exc
    finally:
        client.close()
    if not 200 <= int(getattr(response, "status", 0) or 0) < 300:
        raise MeetingUnavailable(f"upload refused with status {getattr(response, 'status', None)}")


async def put_object(ticket: UploadTicket, content: bytes, *, extension: str) -> str:
    """Upload ``content`` with ``ticket``; return its key.

    The SDK is synchronous, so the upload runs in a worker thread.
    """
    # MD5 as a content address, not a security hash.
    key = f"{ticket.key_prefix}{hashlib.md5(content).hexdigest()}{extension}"
    await asyncio.to_thread(_put, ticket, key, content)
    return key
