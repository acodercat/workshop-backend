"""The single writer of ``admin_audit_log``, and the only way to commit a
console mutation.

The console's central invariant: **every mutation lands with an audit row,
in the same transaction**. :func:`audit` enforces it by shape — it owns
the commit, so the way to persist a change is to make it inside the
block::

    async with audit(
        ctx,
        action=AuditAction.STAFF_UPDATE,
        target_type="staff",
        target_id=account.id,
    ) as draft:
        account.status = body.status
        draft.payload = {"status": {"from": "ACTIVE", "to": "DISABLED"}}
    # the row and the change commit together, or neither does

An endpoint that forgets to audit has nothing to commit its work — a far
better failure than a silent gap in the trail. If the block raises, the
context manager never commits and the change rolls back with it: an
action that failed leaves no row, because it did not happen.

**Do not commit inside the block** — that splits the change from its
audit row into two transactions. Flushing is fine, and is how a create
gets its generated id for ``draft.target_id``.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from app.models.admin_audit_log import AdminAuditLog
from app.services.admin.actions import AuditAction
from app.services.admin.context import AdminContext

logger = logging.getLogger(__name__)

# Payload keys whose values are never stored, matched case-insensitively
# as substrings. An audit trail is read by more people than the database
# itself, so a credential reaching it has effectively been published
# internally. Callers should not be passing these at all — this is the
# backstop, not the policy.
_REDACTED_KEY_PARTS = ("password", "secret", "token", "api_key", "authorization")
_REDACTED_PLACEHOLDER = "[redacted]"


@dataclass
class AuditDraft:
    """The audit row while it is still being filled in.

    Mutable on purpose: the interesting fields are usually facts the
    block *discovers* — the id an insert generated, the value a column
    held before the update.
    """

    target_type: str | None = None
    """Kind of row acted on (``"staff"``, ``"user"``). ``None`` for
    actions with no single target (a sign-in, a bulk export)."""

    target_id: str | int | None = None
    """Its key; stringified on write so int and UUID keys share one
    column."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Parameters and before → after values, flat. Credential-shaped keys
    are redacted on write."""


@asynccontextmanager
async def audit(
    ctx: AdminContext,
    *,
    action: AuditAction,
    target_type: str | None = None,
    target_id: str | int | None = None,
) -> AsyncIterator[AuditDraft]:
    """Run a console mutation and commit it together with its audit row.

    ``target_type`` / ``target_id`` seed the draft when known up front;
    the block can also set them on the yielded draft (a create only
    learns its id after the flush).
    """
    draft = AuditDraft(target_type=target_type, target_id=target_id)

    yield draft

    ctx.db.add(
        AdminAuditLog(
            staff_id=ctx.staff.id,
            action=action.value,
            target_type=draft.target_type,
            target_id=None if draft.target_id is None else str(draft.target_id),
            payload=_redact(draft.payload),
            request_id=ctx.request_id,
            ip_address=ctx.ip_address,
        )
    )
    await ctx.db.commit()

    # Mirrored to the application log so the action is visible in the
    # normal stream too — useful when the console itself is what's broken.
    logger.info(
        "admin action %s by %s on %s:%s",
        action.value,
        ctx.staff.username,
        draft.target_type,
        draft.target_id,
    )


def field_changes(row: object, fields: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The before → after entry of each of *fields* that would move *row*,
    keyed by column — the shape an edit's audit payload records.

    Read before the edit is applied. A field already at its value is left
    out: a "changed X from X to X" entry is noise in a trail whose value is
    that every row means something, and an empty result means the request
    changes nothing and needs no audit row at all.

    Values are stored raw, so every column passed must be JSON-native.
    """
    return {
        name: {"from": getattr(row, name), "to": value}
        for name, value in fields.items()
        if getattr(row, name) != value
    }


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy ``payload`` with credential-looking values replaced.

    Shallow by design: audit payloads are flat parameter maps, and a
    recursive walk would invite callers to dump whole request bodies
    here — exactly what the target fields exist to avoid.
    """
    return {
        key: (
            _REDACTED_PLACEHOLDER
            if any(part in key.lower() for part in _REDACTED_KEY_PARTS)
            else value
        )
        for key, value in payload.items()
    }
