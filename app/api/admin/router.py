"""The console's route table, assembled in one place.

:mod:`app.admin_main` mounts exactly one router. Adding a feature means
importing its module and adding a line here — not editing the ASGI
entrypoint, which should stay about *process* concerns (binding, CORS,
lifespan) rather than growing an import list as the console does.

Prefixes and tags live here rather than on the individual routes, so
every module owns only paths *relative* to its resource
(``""``, ``"/{staff_id}"``) and a resource can be re-mounted by changing
one line. This differs from :mod:`app.api.public.router`, whose modules
spell their full paths — that surface predates any need to move a
resource, and rewriting it is not worth the churn.

``auth`` is mounted bare: sign-in is not a resource under a collection,
and ``/auth/access-tokens`` reads better than a prefix would make it.
"""

from fastapi import APIRouter

from app.api.admin import audit, auth, me, staff

admin_router = APIRouter()

admin_router.include_router(auth.router, tags=["auth"])
admin_router.include_router(me.router, tags=["me"])
admin_router.include_router(staff.router, prefix="/staff", tags=["staff"])
admin_router.include_router(audit.router, prefix="/audit-logs", tags=["audit"])
