"""Schemas for the published-site listing endpoint."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SessionSiteResponse(BaseModel):
    """A row in the cross-session published-site library — one per
    successful ``publish_site``. Sibling of :class:`SessionFileResponse`,
    kept separate because a live site isn't a file artifact.

    ``url`` is *derived* from ``slug`` + the configured preview domain
    (the same way the worker builds it at publish time), never stored, so
    a domain change can't strand a column. ``slug`` is the row's stable
    identity (mirrors how ``SessionFileResponse`` keys on ``file_key``).
    """
    url: str
    slug: str
    title: str | None
    session_id: UUID
    session_title: str | None
    created_at: datetime
