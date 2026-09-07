"""Who is acting, on what session — for one console request.

:class:`AdminContext` is the single value an admin endpoint carries
around: the DB session the mutation runs in and the operator to attribute
it to. It used to carry the correlation id and caller address too, for
the audit row; this deployment keeps no operator trail (revision
``c8f2a5e1d94b``) and nothing read them once that went, so they went too.

It is **pure data**: no FastAPI types, no I/O. The console's HTTP layer
(:mod:`app.api.admin.dependencies`) builds it from the request; the
service layer only reads it. That direction keeps the service layer free
of any dependency on the web framework and testable without fabricating
a ``Request``.
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
    """The request-scoped session the mutation runs in, and commits."""

    staff: Staff
    """The authenticated operator."""
