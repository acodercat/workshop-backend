"""What every admin endpoint depends on: an authorized operator, and the
context their actions get recorded against.

*Authentication* — "who is calling" — is
:func:`app.auth.authenticate_and_get_staff`, next to the other two token
classes. This module holds the two halves that sit on top: whether that
operator's role permits the action, and packaging the result as an
:class:`~app.services.admin.AdminContext` for the service layer.

Both are one dependency because they answer one question per route ("may
this operator do this, and as whom do I record it?"), and because a
context obtainable without the role check is a footgun waiting to be
imported::

    @router.get("")
    async def list_staff(ctx: ViewerContext) -> ...:       # any operator

    @router.post("")
    async def create_staff(ctx: OwnerContext) -> ...:      # OWNER only

The three aliases are the role ladder, pre-built. Endpoints name the
minimum they need in the **signature**, so the permission requirement is
visible where the route is read rather than buried in the body — and a
route cannot end up with no gate at all, because there is no ungated way
to get a context.

These are ``Annotated`` aliases rather than the ``x = Depends(...)``
default the customer-facing endpoints use. That style cannot give one
name to "authenticate + authorize + build context", and spelling the
combination out at every call site is exactly the repetition that lets a
route quietly acquire the wrong gate.
"""

import logging
from ipaddress import ip_address
from typing import Annotated, Any, Callable, Coroutine

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.errors import StaffForbiddenError
from app.models.staff import Staff, StaffRole
from app.services.admin import AdminContext

logger = logging.getLogger(__name__)


def _require(
    min_role: StaffRole,
) -> Callable[..., Coroutine[Any, Any, AdminContext]]:
    """Build the dependency behind one rung of the ladder.

    Private: the three aliases below are the supported entry points, so a
    new route picks from a closed set rather than inventing a gate.
    """

    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db),
        staff: Staff = Depends(authenticate_and_get_staff),
    ) -> AdminContext:
        if not staff.role.covers(min_role):
            logger.warning(
                "Staff %s (%s) denied an action requiring %s",
                staff.username,
                staff.role.value,
                min_role.value,
            )
            raise StaffForbiddenError(
                f"This action requires the {min_role.value} role."
            )
        return build_context(request, db=db, staff=staff)

    return dependency


def build_context(
    request: Request, *, db: AsyncSession, staff: Staff
) -> AdminContext:
    """Assemble the context for an operator already known to the caller.

    The ladder above is the normal way in. This is for the one endpoint
    that cannot use it — sign-in, which only learns who the operator is
    by checking their password, and still needs to audit the attempt.
    Exposed rather than duplicated there so every context in the console
    is stamped with the same request metadata.
    """
    return AdminContext(
        db=db,
        staff=staff,
        request_id=getattr(request.state, "request_id", None),
        ip_address=caller_ip(request),
    )


ViewerContext = Annotated[AdminContext, Depends(_require(StaffRole.VIEWER))]
"""Any signed-in operator — the gate on every read endpoint."""

OperatorContext = Annotated[AdminContext, Depends(_require(StaffRole.OPERATOR))]
"""Day-to-day operations: credit adjustments, withdrawal review, invites."""

OwnerContext = Annotated[AdminContext, Depends(_require(StaffRole.OWNER))]
"""Everything ``OPERATOR`` can do, plus managing staff accounts."""


def caller_ip(request: Request) -> str | None:
    """Best-effort caller address — a *parsed* IP, or the socket peer.

    Public alongside :func:`build_context` for the same reason: sign-in's
    failure path logs the address of an attempt it will never build a
    context for.

    Prefers the first ``X-Forwarded-For`` hop because the console is
    expected to sit behind an internal reverse proxy, where the socket
    peer is the proxy. The hop is client-influenced, which is why it is
    parsed rather than stored as sent: ``admin_audit_log.ip_address`` is
    ``String(45)`` and commits in the same transaction as the action it
    records, so an unparsed over-long value would not make one row less
    informative — it would abort the operator's own action. Parsed, the
    worst a forged header achieves is a wrong but well-formed address in
    one row, which is why this still needs no ``trust_proxy_headers``
    gate the way the rate limiter's key does.

    A hop that is not an address (``unknown``, garbage, anything
    smuggled) falls back to the socket peer.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        candidate = forwarded.split(",", 1)[0].strip()
        try:
            return str(ip_address(candidate))
        except ValueError:
            pass
    return request.client.host if request.client else None
