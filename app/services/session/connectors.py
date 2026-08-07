"""Reading and writing a session's Connector configuration.

Two callers on two layers — the endpoints that show and edit it, and the
enqueue that resolves it into a turn — so the query lives here rather than
beside either of them.

Ownership is not checked here. It is validated once, where the configuration
is written (``_owned_mcp_server_ids``), and enforced again where it is spent:
``_resolve_mcp_servers`` re-reads every row by id and drops what is no longer
this user's or no longer connected.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_mcp_server import SessionMCPServer


async def configured_ids(db: AsyncSession, session_id: uuid.UUID) -> list[int]:
    """The ``mcp_server`` ids this session is configured to use.

    Sorted, because the table does not keep the order they were picked in and
    an arbitrary one would make the response non-deterministic — a diff-noisy
    payload and a flaky test for no gain. Nothing downstream reads the order.
    """
    rows = await db.execute(
        select(SessionMCPServer.mcp_server_id)
        .where(SessionMCPServer.session_id == session_id)
        .order_by(SessionMCPServer.mcp_server_id)
    )
    return list(rows.scalars())


async def replace(
    db: AsyncSession, session_id: uuid.UUID, mcp_server_ids: list[int]
) -> None:
    """Make *mcp_server_ids* the session's whole configuration.

    Replace rather than diff: the client sends the set it wants, and the set is
    small enough that computing the delta would be more code than it saves.

    Both statements are Core, so the delete provably precedes the insert. Adding
    the rows through the session instead would leave that order to the unit of
    work — and a set that overlaps the old one re-inserts keys the delete has
    not run yet, which is a primary-key violation rather than a replacement.

    Does not commit: the caller owns the transaction, so the two land together
    or not at all.
    """
    await db.execute(
        delete(SessionMCPServer).where(SessionMCPServer.session_id == session_id)
    )
    if mcp_server_ids:
        await db.execute(
            insert(SessionMCPServer),
            [
                {"session_id": session_id, "mcp_server_id": mcp_server_id}
                for mcp_server_id in mcp_server_ids
            ],
        )
