"""Who is acting, on what session, from where — for one console request.

:class:`AdminContext` is the single value an admin endpoint carries
around. It exists because the console's central invariant (*every
mutation is audited*) needs four things at once — the DB session the
mutation runs in, the operator to attribute it to, and the correlation id
plus caller address that make the audit row worth reading. Threading four
parameters through every call site is how one of them ends up omitted.

It is **pure data**: no FastAPI types, no I/O. The console's HTTP layer
(:mod:`app.api.admin.dependencies`) builds it from the request; the
service layer only reads it. That direction is what keeps
:mod:`app.services.admin.audit` free of any dependency on the web
framework and testable without fabricating a ``Request``.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.staff import Staff


@dataclass(frozen=True, slots=True)
class AdminContext:
    """One authenticated console request.

    Frozen: it describes a request that is already underway, and nothing
    downstream has any business swapping the operator or the session.
    """

    db: AsyncSession
    """The request-scoped session. Mutation and audit row share it, which
    is what lets them commit atomically."""

    staff: Staff
    """The authenticated operator, already role-checked by the dependency
    that built this context."""

    request_id: str | None
    """Correlation id from ``RequestIDMiddleware`` — ties the audit row to
    the full request trace in the logs."""

    ip_address: str | None
    """Best-effort caller address, for after-the-fact review."""
