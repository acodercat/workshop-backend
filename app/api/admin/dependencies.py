"""What every admin endpoint depends on: an authenticated operator and
the session their writes run in.

*Authentication* — "who is calling" — is
:func:`app.auth.authenticate_and_get_staff`, next to the other two token
classes. This module packages the result as an
:class:`~app.services.admin.AdminContext` for the service layer::

    @router.get("/staff")
    async def list_staff(ctx: StaffContext) -> ...

There are **no roles**: every active operator may use every endpoint (a
v1 decision recorded in ``docs/admin-console.md`` — the operator team is
small and fully trusted, and named). If
tiers are ever needed, this module is where a role gate would return.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_staff
from app.models.staff import Staff
from app.services.admin import AdminContext


async def _authenticated_context(
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(authenticate_and_get_staff),
) -> AdminContext:
    return AdminContext(db=db, staff=staff)


StaffContext = Annotated[AdminContext, Depends(_authenticated_context)]
"""A signed-in, active operator plus the session and request metadata
their actions are recorded against. The only way into an admin endpoint."""


def caller_ip(request: Request) -> str | None:
    """Best-effort caller address for the sign-in refusal log line, which
    is the one place this deployment records where a request came from.

    Prefers the first ``X-Forwarded-For`` hop because the console is
    expected to sit behind an internal reverse proxy, where the socket
    peer is the proxy. Unlike the rate limiter — where a spoofable header
    would let a client dodge a limit — a wrong value here only makes one
    log line less informative, so this is not gated on
    ``trust_proxy_headers``.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None
