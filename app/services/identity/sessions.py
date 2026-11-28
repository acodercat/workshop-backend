"""The life of an ``identity_session`` row: opened, re-confirmed, revoked.

A session here is only ever as valid as the provider's session behind it.
Sign-out or a ban at the provider has to end ours too, and the provider
has no way to tell us — so we ask, on a schedule, from inside ordinary
requests (:func:`ensure_confirmed`). The whole of this module's care goes
into doing that without making the provider a dependency of every request:

- **At most one call per session per interval.** A request that finds the
  session due claims the check with a single conditional ``UPDATE`` before
  calling out. Concurrent requests lose the claim and proceed on the
  existing confirmation instead of each making the same call.
- **An outage is not a rejection.** Only the provider *saying* the session
  is over revokes it. If the provider cannot be reached the session runs
  on its last confirmation for a bounded grace period, then is refused
  with a 503 — never a 401, which clients read as "signed out".
- **The claim is committed before the call goes out.** The claim and the
  result are each written in a short session of their own. A claim left
  uncommitted would hold its row lock for the length of the provider's
  timeout, and every other request on that session would queue behind it
  instead of losing the claim and moving on.

This module must not import :mod:`app.auth` — it is imported by it.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.errors import IdentityProviderUnavailableError
from app.integrations.meeting.transport import MeetingSession
from app.models.identity_session import IdentitySession
from app.models.user import User
from app.models.user_identity import UserIdentity
from app.services.identity.credentials import CredentialUnreadable, seal, unseal
from app.services.identity.provider import CredentialRejected, MeetingIdentityProvider
from config import settings

logger = logging.getLogger(__name__)


class SessionEnded(Exception):
    """The session is over — revoked just now, or found to be.

    Internal on purpose: the caller (:mod:`app.auth`) answers it with the
    same opaque 401 as any other bad token, so a client cannot tell a
    revoked session from a forged one.
    """


async def open_session(
    db: AsyncSession,
    *,
    user: User,
    identity: UserIdentity,
    credential: MeetingSession,
    lifetime: timedelta,
) -> IdentitySession:
    """Record a new session for ``user``. Adds the row; the caller commits."""
    now = datetime.now(UTC)
    # Sweep this user's dead rows while we are here. Sessions are only
    # ever read by id, so stale ones cost nothing but space — and sign-in
    # is the one moment a bounded cleanup is naturally per-user.
    await db.execute(
        delete(IdentitySession).where(
            IdentitySession.user_id == user.id,
            or_(IdentitySession.expires_at <= now, IdentitySession.revoked_at.is_not(None)),
        )
    )
    session = IdentitySession(
        id=uuid.uuid4(),
        user_id=user.id,
        identity_id=identity.id,
        credential=seal(credential),
        checked_at=now,
        verified_at=now,
        expires_at=now + lifetime,
    )
    db.add(session)
    return session


def is_due(checked_at: datetime, *, now: datetime) -> bool:
    """Whether a session is due to be re-confirmed with the provider. A
    caller holding a transaction ends it first when this is true, so no
    connection idles while the provider is asked."""
    return now - checked_at >= timedelta(seconds=settings.identity_reverify_interval_seconds)


def is_live(session: IdentitySession, *, now: datetime) -> bool:
    """Whether the row still authorizes anything at all, before any question
    of how recently the provider confirmed it."""
    return session.revoked_at is None and session.expires_at > now


def _live() -> tuple[ColumnElement[bool], ...]:
    """:func:`is_live` as SQL, for the statements that act on a session."""
    return (IdentitySession.revoked_at.is_(None), IdentitySession.expires_at > func.now())


async def ensure_confirmed(
    *,
    session_id: uuid.UUID,
    subject: str,
    checked_at: datetime,
    verified_at: datetime,
    provider: MeetingIdentityProvider,
) -> None:
    """Make sure the provider's word on this session is recent enough.

    Takes the row's fields rather than the row, and no ``AsyncSession``:
    whatever it writes goes through short sessions of its own (see the
    module docstring), so it does not matter what kind of session the
    caller read the row with.

    Raises :class:`SessionEnded` if the provider no longer honours the
    session, :class:`~app.errors.IdentityProviderUnavailableError` if it
    has been unreachable for longer than the grace period.
    """
    now = datetime.now(UTC)
    if is_due(checked_at, now=now):
        due_before = now - timedelta(seconds=settings.identity_reverify_interval_seconds)
        confirmed_at = await _reverify(
            session_id=session_id, subject=subject, due_before=due_before, provider=provider
        )
        if confirmed_at is not None:
            return
    # Not due, or due and unconfirmed (the claim was lost to a concurrent
    # request, or the provider did not answer). Either way the session
    # stands on its last confirmation — if that is recent enough.
    if now - verified_at > timedelta(seconds=settings.identity_reverify_grace_seconds):
        raise IdentityProviderUnavailableError(
            "The identity provider could not confirm this session."
        )


async def _reverify(
    *,
    session_id: uuid.UUID,
    subject: str,
    due_before: datetime,
    provider: MeetingIdentityProvider,
) -> datetime | None:
    """Ask the provider about one session. Returns when it was confirmed,
    or ``None`` if this call confirmed nothing."""
    sealed = await _claim(session_id, due_before=due_before)
    if sealed is None:
        return None

    try:
        credential = unseal(sealed)
    except CredentialUnreadable:
        # The encryption key changed or the row is damaged. Nothing can
        # ever confirm this session again; end it now rather than let it
        # coast to the end of the grace period.
        logger.warning("identity session %s holds an unreadable credential", session_id)
        await end_session(session_id)
        raise SessionEnded from None

    try:
        grant = await provider.verify(
            credential, timeout=settings.identity_reverify_timeout_seconds
        )
    except CredentialRejected:
        if await end_refused_session(session_id, refused=credential):
            raise SessionEnded from None
        # A concurrent call rotated the credential while this one asked
        # with the old value: the refusal was of a superseded credential,
        # and the stored one is next asked an interval from now.
        return None
    except IdentityProviderUnavailableError:
        # The claim already moved ``checked_at``, so the next attempt is an
        # interval away — an outage costs one timeout per session per
        # interval, not one per request.
        logger.warning("identity provider unavailable re-verifying session %s", session_id)
        return None

    if grant.identity.subject != subject:
        # The credential is live but answers for a different account. That
        # should be impossible; if it happens, the one thing it must not
        # do is keep authorizing this one.
        logger.error("identity session %s re-verified as a different subject", session_id)
        await end_session(session_id)
        raise SessionEnded

    confirmed_at = datetime.now(UTC)
    await _confirm(
        session_id,
        confirmed_at=confirmed_at,
        sealed=sealed,
        rotated=grant.credential if grant.credential != credential else None,
    )
    return confirmed_at


async def _claim(session_id: uuid.UUID, *, due_before: datetime) -> str | None:
    """Take the right to re-verify, returning the sealed credential.

    One statement, so it is atomic across requests *and* processes: of any
    number racing, exactly one sees ``checked_at`` still old, moves it, and
    gets a row back. ``None`` means another request has it.

    Raises :class:`SessionEnded` when the claimed row has been revoked
    since the caller read it, so a request that read the row just before a
    sign-out is refused rather than let through on its last confirmation.
    """
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                update(IdentitySession)
                .where(
                    IdentitySession.id == session_id,
                    IdentitySession.checked_at <= due_before,
                )
                .values(checked_at=datetime.now(UTC))
                .returning(IdentitySession.credential, IdentitySession.revoked_at)
            )
        ).one_or_none()
        await db.commit()
    if row is None:
        return None
    if row.revoked_at is not None:
        raise SessionEnded
    return row.credential


async def _confirm(
    session_id: uuid.UUID,
    *,
    confirmed_at: datetime,
    sealed: str,
    rotated: MeetingSession | None,
) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(IdentitySession)
            .where(IdentitySession.id == session_id)
            .values(verified_at=confirmed_at)
        )
        if rotated is not None:
            # Compare-and-swap on the value we read: if anything else
            # replaced the credential while we were asking, theirs is
            # newer than ours and must not be overwritten.
            await db.execute(
                update(IdentitySession)
                .where(IdentitySession.id == session_id, IdentitySession.credential == sealed)
                .values(credential=seal(rotated))
            )
        await db.commit()


@dataclass(frozen=True)
class SessionCredential:
    """A live session's Meeting session, for acting as its user there.
    ``sealed`` is the stored form, kept as the version to compare-and-swap
    against in :func:`replace_credential`."""

    # The account's id at Meeting.
    subject: str
    credential: MeetingSession
    sealed: str


async def load_credential(session_id: uuid.UUID) -> SessionCredential | None:
    """The credential behind a live session, or ``None`` if the session is
    revoked, expired or gone, or its credential cannot be read. Reads in a
    session of its own."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(IdentitySession.credential, UserIdentity.subject)
                .join(UserIdentity, UserIdentity.id == IdentitySession.identity_id)
                .where(IdentitySession.id == session_id, *_live())
            )
        ).one_or_none()
    if row is None:
        return None
    try:
        credential = unseal(row.credential)
    except CredentialUnreadable:
        return None
    return SessionCredential(subject=row.subject, credential=credential, sealed=row.credential)


async def replace_credential(
    session_id: uuid.UUID, *, expected: str, credential: MeetingSession
) -> str | None:
    """Store a rotated credential, compare-and-swap on the sealed value the
    caller read. Returns the new sealed value, or ``None`` if the row had
    already moved on (another request rotated it first, or it was revoked
    or expired) — in which case the stored one is newer and must be kept."""
    sealed = seal(credential)
    async with AsyncSessionLocal() as db:
        stored = (
            await db.execute(
                update(IdentitySession)
                .where(
                    IdentitySession.id == session_id,
                    *_live(),
                    IdentitySession.credential == expected,
                )
                .values(credential=sealed)
                .returning(IdentitySession.id)
            )
        ).scalar_one_or_none()
        await db.commit()
    return sealed if stored is not None else None


async def end_session(session_id: uuid.UUID) -> None:
    """Revoke a session in a transaction of its own, whatever credential it
    holds — for a session nothing can ever confirm again."""
    async with AsyncSessionLocal() as db:
        await revoke_session(db, session_id)
        await db.commit()


async def end_refused_session(session_id: uuid.UUID, *, refused: MeetingSession) -> bool:
    """Revoke a session because the provider refused ``refused``, the
    credential a call presented for it. Returns whether the session is over.

    Compare-and-swap on that credential: Meeting rotates the session cookie,
    so a call made with a value another call has since replaced can be
    refused while the stored one is live. When the stored credential is no
    longer ``refused`` the session stands and this returns ``False``. The
    row is locked while compared, so a rotation cannot land in between.
    """
    async with AsyncSessionLocal() as db:
        sealed = (
            await db.execute(
                select(IdentitySession.credential)
                .where(IdentitySession.id == session_id, IdentitySession.revoked_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sealed is None:
            return True
        try:
            superseded = unseal(sealed) != refused
        except CredentialUnreadable:
            # Nothing could act with the stored one either.
            superseded = False
        if superseded:
            return False
        await revoke_session(db, session_id)
        await db.commit()
    return True


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> str | None:
    """Mark a session revoked. Returns its sealed credential the first time
    — so the caller can end the provider's side too — and ``None`` if it
    was already revoked or never existed. The caller commits."""
    return (
        await db.execute(
            update(IdentitySession)
            .where(IdentitySession.id == session_id, IdentitySession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
            .returning(IdentitySession.credential)
        )
    ).scalar_one_or_none()
