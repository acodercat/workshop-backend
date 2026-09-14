"""Reading and writing a scheduled task's attachments.

Two callers on two layers — the endpoints that show and edit the list, and
the dispatcher that hands it to every session the task creates — so the
queries live here rather than beside either of them. Mirrors
:mod:`app.services.schedule.connectors`.

Unlike its Connector twin it stores a size, because an attachment is an
object in OSS rather than a row — and it never stores the client's number
for it. ``file_storage.confirmed_sizes`` is what makes the persisted value
trustworthy, and the per-user quota sums that column, so a claim taken on
faith would be a way to exceed it.

Ownership is checked with the same predicate a message attachment uses
(``assert_files_owned_by``), and at save time. It is re-checked at every
firing anyway, but a foreign key caught only there would fail each
occurrence rather than the one request that could have been told why.

It also takes every attachment off the orphan-reaping rule, and — unlike
the message path — *before* the transaction commits, failing the save if
storage refuses. A presigned upload is born tagged ``lifecycle=orphan`` and a
bucket lifecycle rule deletes it about a day later. A message's attachment
is read within the turn that binds it, so clearing that tag can be
best-effort; a scheduled task may not fire for days, so an attachment still
tagged is one the task will find deleted. Clearing first turns the only
failure left into its harmless opposite: a save that then fails to commit
leaves an untagged object nothing references, which costs storage and not a
file somebody was counting on.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule_file import ScheduleFile
from app.schemas.session import FileRef
from app.services import file_storage
from app.services.session.files import assert_files_owned_by


async def attached(db: AsyncSession, schedule_id: int) -> list[ScheduleFile]:
    """This schedule's attachments, oldest first.

    Ordered so the agent sees them in the order they were added, which is
    the only order the owner ever expressed.
    """
    rows = await db.execute(
        select(ScheduleFile)
        .where(ScheduleFile.schedule_id == schedule_id)
        .order_by(ScheduleFile.id)
    )
    return list(rows.scalars())


async def attached_by_schedule(
    db: AsyncSession, schedule_ids: Sequence[int]
) -> dict[int, list[ScheduleFile]]:
    """:func:`attached` for many schedules, in one round trip.

    The management list renders every schedule the owner has; asking per
    card would put a query behind each one. Schedules with nothing attached
    are absent rather than mapped to ``[]`` — callers read through
    ``.get(id, [])``, which also keeps this from inventing rows for ids that
    do not exist.
    """
    if not schedule_ids:
        return {}
    rows = await db.execute(
        select(ScheduleFile)
        .where(ScheduleFile.schedule_id.in_(schedule_ids))
        .order_by(ScheduleFile.schedule_id, ScheduleFile.id)
    )
    by_schedule: dict[int, list[ScheduleFile]] = {}
    for row in rows.scalars():
        by_schedule.setdefault(row.schedule_id, []).append(row)
    return by_schedule


async def replace(
    db: AsyncSession,
    schedule_id: int,
    user_id: int,
    files: list[FileRef],
) -> None:
    """Make *files* the schedule's whole attachment list.

    Every kept attachment is taken off the orphan-reaping rule again, not
    only the new ones. The call is idempotent, and it is what repairs an
    attachment an earlier save could not clear: resaving the task is the
    remedy the refusal points its owner at.

    Does not commit: the caller owns the transaction.
    """
    wanted: dict[str, FileRef] = {}
    for file_ref in files:
        wanted.setdefault(file_ref.file_key, file_ref)
    # At save time, not first firing: a foreign key stored here would fail
    # every occurrence instead of the one request that could be told why.
    await assert_files_owned_by(db, user_id=user_id, file_keys=wanted.keys())

    existing = {row.file_key: row for row in await attached(db, schedule_id)}
    for file_key in existing.keys() - wanted.keys():
        await db.delete(existing[file_key])
    to_add = [file_ref for key, file_ref in wanted.items() if key not in existing]
    sizes = await file_storage.confirmed_sizes(
        file_storage.SESSION_FILE_POLICY, [f.file_key for f in to_add]
    )
    await file_storage.clear_orphan_tags(file_storage.SESSION_FILE_POLICY, list(wanted))

    for file_ref, size in zip(to_add, sizes):
        db.add(
            ScheduleFile(
                schedule_id=schedule_id,
                user_id=user_id,
                file_key=file_ref.file_key,
                filename=file_ref.filename,
                mime_type=file_ref.mime_type,
                size_bytes=size,
            )
        )
