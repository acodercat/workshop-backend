"""From an external identity to the local account it owns.

The lookup key is ``(provider, issuer, subject)`` and only that. An email
match is never used to find an account: the provider may reassign an
address, and an account found by one would follow it to its next holder.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.user_identity import UserIdentity
from app.services.identity.provider import ExternalIdentity
from app.services.identity.provisioning import provision_user

logger = logging.getLogger(__name__)


async def _find(db: AsyncSession, identity: ExternalIdentity) -> tuple[UserIdentity, User] | None:
    row = (
        await db.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(
                UserIdentity.provider == identity.provider,
                UserIdentity.issuer == identity.issuer,
                UserIdentity.subject == identity.subject,
            )
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


async def resolve_user(
    db: AsyncSession, identity: ExternalIdentity
) -> tuple[User, UserIdentity, bool]:
    """Return the account ``identity`` owns, creating it on first sight.

    The third element says whether it was created by this call. Flushes,
    never commits.

    A known identity has its email refreshed from the provider: the local
    copy exists to be shown, and the provider is where it is edited. The
    display name is *not* refreshed — it is seeded once and then the
    user's to change here, and overwriting it at every sign-in would undo
    them each time.
    """
    found = await _find(db, identity)
    if found is None:
        try:
            user = await provision_user(db, name=identity.display_name, email=identity.email)
            link = UserIdentity(
                user_id=user.id,
                provider=identity.provider,
                issuer=identity.issuer,
                subject=identity.subject,
            )
            db.add(link)
            await db.flush()
        except IntegrityError:
            # Two first sign-ins for one identity, racing. Both provisioned
            # an account; ``uq_user_identity_subject`` let one link through
            # and this is the other. Its half-built account goes with the
            # rollback, and the winner's is the one that exists.
            await db.rollback()
            found = await _find(db, identity)
            if found is None:
                raise
            logger.info("identity first sign-in race: provider=%s", identity.provider)
        else:
            logger.info(
                "user provisioned: id=%s provider=%s organization_id=%s",
                user.id,
                identity.provider,
                user.organization_id,
            )
            return user, link, True

    link, user = found
    if identity.email is not None:
        user.email = identity.email
    return user, link, False
