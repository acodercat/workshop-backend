"""Acting as the signed-in user at Meeting.

The meeting workspace reads and writes the user's meetings *at Meeting*, as
them. It can, because sign-in is federated to Meeting and the user's session
holds their Meeting session (:mod:`app.services.identity.sessions`). This
module turns it into a :class:`~app.integrations.meeting.transport.MeetingActor`
and nothing else sees it.

When Meeting rotates the session cookie during a call, the new value is
written back before the call returns, so the next request — this user's,
on any process — presents the live one.
"""

import uuid
from dataclasses import dataclass

from app.integrations.meeting.transport import MeetingActor, MeetingSession
from app.services.identity.sessions import load_credential, replace_credential


class DelegationUnavailable(Exception):
    """This session holds no Meeting session that can be used — it has
    ended, or its credential is unreadable.

    Not an ``AppError``: what it means depends on who asked. A request that
    just authenticated has met a server-side inconsistency; the archiver has
    met a user who signed out.
    """


@dataclass(frozen=True)
class Delegation:
    actor: MeetingActor
    # The account's id at Meeting — some Meeting calls name it explicitly.
    account_id: str


async def meeting_actor(session_id: uuid.UUID) -> Delegation:
    """The Meeting account behind ``session_id``, ready to make calls as."""
    stored = await load_credential(session_id)
    if stored is None:
        raise DelegationUnavailable(str(session_id))
    sealed = stored.sealed

    async def on_rotate(old: MeetingSession, new: MeetingSession) -> None:
        nonlocal sealed
        replaced = await replace_credential(session_id, expected=sealed, credential=new)
        if replaced is not None:
            sealed = replaced

    return Delegation(
        MeetingActor(stored.credential, on_rotate=on_rotate), account_id=stored.subject
    )
