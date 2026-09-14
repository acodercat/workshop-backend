"""Which Connectors belong to a user.

Two places let a client name Connector ids — creating a session and saving
a scheduled task — and both must refuse an id the caller does not own. The
whole rule lives here, raising the typed error the shared handler renders,
so neither endpoint carries a copy: a second copy is how a later change (a
soft-delete column, an organization scope) gets applied to one caller and
forgotten at the other.

Ownership only — never ``connected``. Configuration records intent, and a
Connector disconnected afterwards must not retroactively invalidate it.
Liveness is re-checked where the configuration is spent, on every turn.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConnectorNotOwnedError
from app.models.mcp_server import MCPServer


async def require_owned_connectors(
    db: AsyncSession, *, user_id: int, ids: Sequence[int]
) -> list[int]:
    """*ids* de-duplicated in the order given, if every one is the caller's.

    Raises :class:`ConnectorNotOwnedError` otherwise. De-duplicated because
    the same Connector twice is one Connector — and because both binding
    tables key on it, so a duplicate would be a primary-key violation.
    """
    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids:
        return []
    owned = set(
        (
            await db.execute(
                select(MCPServer.id).where(
                    MCPServer.id.in_(unique_ids),
                    MCPServer.user_id == user_id,
                )
            )
        ).scalars()
    )
    unknown = [server_id for server_id in unique_ids if server_id not in owned]
    if unknown:
        raise ConnectorNotOwnedError(f"Unknown connector ids: {unknown}")
    return unique_ids
