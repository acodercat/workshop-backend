"""The console's route table, assembled in one place.

:mod:`app.admin_main` mounts exactly one router. Adding a feature means
importing its module and adding a line here — not editing the ASGI
entrypoint, which should stay about *process* concerns (binding, CORS,
lifespan) rather than growing an import list as the console does.

Every module spells its **full paths** (``/staff/{staff_id}``, never a
bare ``""`` re-rooted by an include-time prefix) and carries its own
tag — the same convention as :mod:`app.api.public.router`, so a route's
URL is readable where the route is defined on both surfaces.
"""

from fastapi import APIRouter

from app.api.admin import (
    auth,
    category,
    connector,
    feedback,
    invite,
    me,
    meeting_settings,
    message_feedback,
    session,
    skill,
    staff,
    stats,
    user,
)

admin_router = APIRouter()

admin_router.include_router(auth.router)
admin_router.include_router(me.router)
admin_router.include_router(stats.router)
admin_router.include_router(staff.router)
admin_router.include_router(user.router)
admin_router.include_router(session.router)
admin_router.include_router(feedback.router)
admin_router.include_router(message_feedback.router)
admin_router.include_router(invite.router)
admin_router.include_router(skill.router)
admin_router.include_router(connector.router)
admin_router.include_router(category.router)
admin_router.include_router(meeting_settings.router)
