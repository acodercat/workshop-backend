"""Sign-in and sign-out, end to end.

The endpoints in :mod:`app.api.public.auth` are thin adapters over these
two functions; the order of steps inside them is the part worth reading.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, is_user_active
from app.errors import AccountInactiveError, IdentityProviderUnavailableError
from app.integrations.meeting.transport import MeetingSession
from app.models.user import User
from app.services.identity.credentials import CredentialUnreadable, unseal
from app.services.identity.federation import resolve_user
from app.services.identity.provider import MeetingIdentityProvider
from app.services.identity.provisioning import grant_welcome_credits
from app.services.identity.sessions import open_session, revoke_session
from config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignedIn:
    access_token: str
    user: User


async def sign_in(
    db: AsyncSession, provider: MeetingIdentityProvider, *, account: str, password: str
) -> SignedIn:
    """Authenticate with the provider and open a local session.

    1. The provider checks the credentials. Nothing local has happened
       yet, so a refusal leaves no trace — and no database connection is
       held while it answers.
    2. The identity is resolved to a local account, created if this is the
       first time it has been seen.
    3. The account must be ``ACTIVE`` *here*. The provider vouching for
       someone does not override an operator disabling them.
    4. A session row is opened holding the provider's credential, and the
       access token is minted naming it. Token and row share one lifetime.
    5. Only after all of that commits is a new account's welcome grant
       attempted (see :func:`~.provisioning.grant_welcome_credits` for why
       it cannot ride in the same transaction).

    If anything after step 1 fails, the session the provider just opened
    is ended there too, rather than left open with nothing here using it.
    """
    grant = await provider.authenticate(account.strip(), password)
    try:
        user, identity, created = await resolve_user(db, grant.identity)
        if not is_user_active(user):
            raise AccountInactiveError("Account is not active")

        lifetime = timedelta(minutes=settings.access_token_expire_minutes)
        session = await open_session(
            db, user=user, identity=identity, credential=grant.credential, lifetime=lifetime
        )
        user.last_login_at = datetime.now(UTC)
        access_token = create_access_token(
            data={"sub": user.username, "sid": str(session.id)}, expires_delta=lifetime
        )
        user_id = user.id
        await db.commit()
    except Exception:
        await _end_at_provider(provider, grant.credential)
        raise

    if created:
        await grant_welcome_credits(db, user_id=user_id)
    await db.refresh(user)
    return SignedIn(access_token=access_token, user=user)


async def sign_out(
    db: AsyncSession, provider: MeetingIdentityProvider, *, session_id: uuid.UUID
) -> None:
    """End a session here, then at the provider.

    In that order, and with the local revocation committed first: once
    this returns the access token is dead whatever the provider did. Its
    side is best-effort — a provider that cannot be reached must not be
    able to keep someone signed in here — and a session left alive over
    there is harmless to us, since nothing local refers to it any more.
    """
    sealed = await revoke_session(db, session_id)
    await db.commit()
    if sealed is None:
        return
    try:
        credential = unseal(sealed)
    except CredentialUnreadable:
        logger.warning("could not end the provider session behind %s", session_id)
        return
    await _end_at_provider(provider, credential)


async def _end_at_provider(provider: MeetingIdentityProvider, credential: MeetingSession) -> None:
    """End a session at the provider, best-effort: nothing here depends on
    it, so a provider that cannot be reached only leaves its side open."""
    try:
        await provider.revoke(credential)
    except IdentityProviderUnavailableError:
        logger.warning("could not end a session at the identity provider")
