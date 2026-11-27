"""The paths whose correctness is the database's: unique-constraint races,
compare-and-swap writes, ``SKIP LOCKED`` claims fenced by a lease token.

Opt-in (``RUN_DB_TESTS=1``) against a scratch database at alembic head;
every test creates its own rows under fresh identities and leaves them.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.integrations.meeting.transport import MeetingSession
from app.models.meeting_recording import (
    ArchiveState,
    MeetingRecording,
    RecordingKind,
    RecordingStatus,
)
from app.services.identity import signin
from app.services.identity.provider import ExternalIdentity, ProviderGrant
from app.services.meeting.delegation import meeting_actor
from app.workers import meeting_archiver
from config import settings
from tests.conftest import requires_database

pytestmark = requires_database


def _provider(subject: str) -> MagicMock:
    provider = MagicMock()
    provider.authenticate = AsyncMock(
        return_value=ProviderGrant(
            identity=ExternalIdentity(
                provider="meeting",
                issuer="https://meeting.test",
                subject=subject,
                email=None,
                display_name="Tester",
            ),
            credential=MeetingSession("p1", "d1"),
        )
    )
    return provider


async def _sign_in(provider: MagicMock) -> signin.SignedIn:
    async with AsyncSessionLocal() as db:
        return await signin.sign_in(db, provider, account="a", password="b")


async def test_racing_first_sign_ins_create_one_account() -> None:
    provider = _provider(f"race-{uuid.uuid4()}")

    results = await asyncio.gather(*(_sign_in(provider) for _ in range(4)))

    assert len({result.user.id for result in results}) == 1


async def _session_id(signed_in: signin.SignedIn) -> uuid.UUID:
    claims = jwt.decode(
        signed_in.access_token, settings.secret_key, algorithms=[settings.algorithm]
    )
    return uuid.UUID(claims["sid"])


async def test_a_rotated_credential_is_written_back_once() -> None:
    session_id = await _session_id(await _sign_in(_provider(f"rotate-{uuid.uuid4()}")))
    first = await meeting_actor(session_id)
    second = await meeting_actor(session_id)
    assert first.actor.on_rotate and second.actor.on_rotate

    # Both copies saw "p1"; the first rotation wins, the second — made
    # against a value that has since changed — must not overwrite it.
    await first.actor.on_rotate(first.actor.session, MeetingSession("p2", "d1"))
    await second.actor.on_rotate(second.actor.session, MeetingSession("stale", "d1"))

    assert (await meeting_actor(session_id)).actor.session.passport == "p2"


async def _queued_recording(user_id: int) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        row = MeetingRecording(
            user_id=user_id,
            kind=RecordingKind.LIVE,
            status=RecordingStatus.ARCHIVING,
            title="t",
            meeting_time=datetime.now(UTC),
            units=1,
            samples=100,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _claim_mine(recording_id: uuid.UUID) -> meeting_archiver._Claim:
    # Other tests' rows may be queued too; claim until ours comes up.
    while (claim := await meeting_archiver._claim()) is not None:
        if claim.recording_id == recording_id:
            return claim
    raise AssertionError("the recording was never claimed")


async def test_an_archiver_whose_lease_lapsed_cannot_file_or_settle() -> None:
    user_id = (await _sign_in(_provider(f"lease-{uuid.uuid4()}"))).user.id
    recording_id = await _queued_recording(user_id)
    stale = await _claim_mine(recording_id)

    # The lease lapses and a second archiver takes the row over.
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MeetingRecording)
            .where(MeetingRecording.id == recording_id)
            .values(lease_until=None)
        )
        await db.commit()
    current = await _claim_mine(recording_id)

    # The first can neither mark the intent to file nor record an outcome.
    with pytest.raises(meeting_archiver._LostClaim):
        await meeting_archiver._mark_requested(stale)
    await meeting_archiver._settle(stale, archived_meeting_id="duplicate")

    # The second still can, exactly once.
    await meeting_archiver._mark_requested(current)
    with pytest.raises(meeting_archiver._LostClaim):
        await meeting_archiver._mark_requested(current)
    await meeting_archiver._settle(current, archived_meeting_id="the-one")

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(MeetingRecording).where(MeetingRecording.id == recording_id))
        ).scalar_one()
    assert row.status == RecordingStatus.ARCHIVED
    assert row.archive_state == ArchiveState.DONE
    assert row.archived_meeting_id == "the-one"
    assert row.lease_token is None
