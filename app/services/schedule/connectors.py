"""Reading and writing a scheduled task's Connector configuration.

Two callers on two layers — the endpoints that show and edit it, and the
dispatcher that spends it when the task fires — so the queries live here
rather than beside either of them. Mirrors
:mod:`app.services.session.connectors`, minus its ``add`` / ``remove``:
a schedule is edited through a form that submits the whole set, not
through live switches, so there is no single-Connector path to make
idempotent.

Ownership is not checked here. It is validated once, where the
configuration is written
(:func:`app.services.connector.ownership.require_owned_connectors`), and
enforced again where it is spent: each firing copies these ids onto a new
session, and that session's enqueue re-reads every row by id and drops
what is no longer this user's or no longer connected.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule_mcp_server import ScheduleMCPServer


async def configured_ids(db: AsyncSession, schedule_id: int) -> list[int]:
    """The ``mcp_server`` ids this schedule is configured to use.

    Sorted, because the table does not keep the order they were picked in
    and an arbitrary one would make the response non-deterministic — a
    diff-noisy payload and a flaky test for no gain. Nothing downstream
    reads the order.
    """
    rows = await db.execute(
        select(ScheduleMCPServer.mcp_server_id)
        .where(ScheduleMCPServer.schedule_id == schedule_id)
        .order_by(ScheduleMCPServer.mcp_server_id)
    )
    return list(rows.scalars())


async def configured_ids_by_schedule(
    db: AsyncSession, schedule_ids: Sequence[int]
) -> dict[int, list[int]]:
    """:func:`configured_ids` for many schedules, in one round trip.

    The management list renders every schedule the owner has, and asking
    per card would put a query behind each one — the same N+1 the list
    avoids by reading every card's latest run in one query. Schedules with nothing
    configured are absent from the result rather than mapped to ``[]``;
    callers read through ``.get(id, [])``, which is also what keeps this
    from inventing rows for ids that do not exist.
    """
    if not schedule_ids:
        return {}
    rows = await db.execute(
        select(ScheduleMCPServer.schedule_id, ScheduleMCPServer.mcp_server_id)
        .where(ScheduleMCPServer.schedule_id.in_(schedule_ids))
        .order_by(ScheduleMCPServer.schedule_id, ScheduleMCPServer.mcp_server_id)
    )
    by_schedule: dict[int, list[int]] = {}
    for schedule_id, mcp_server_id in rows:
        by_schedule.setdefault(schedule_id, []).append(mcp_server_id)
    return by_schedule


async def replace(db: AsyncSession, schedule_id: int, mcp_server_ids: list[int]) -> None:
    """Make *mcp_server_ids* the schedule's whole configuration.

    Both statements are Core, so the delete provably precedes the insert.
    Adding the rows through the session instead would leave that order to
    the unit of work — and a set that overlaps the old one re-inserts keys
    the delete has not run yet, which is a primary-key violation rather
    than a replacement.

    Does not commit: the caller owns the transaction, so the two land
    together or not at all.
    """
    await db.execute(delete(ScheduleMCPServer).where(ScheduleMCPServer.schedule_id == schedule_id))
    if mcp_server_ids:
        await db.execute(
            insert(ScheduleMCPServer),
            [
                {"schedule_id": schedule_id, "mcp_server_id": mcp_server_id}
                for mcp_server_id in mcp_server_ids
            ],
        )
