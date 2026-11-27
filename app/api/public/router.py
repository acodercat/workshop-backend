"""The customer API's route table, assembled in one place.

:mod:`app.public_main` mounts exactly one router — the same shape as the
console's :mod:`app.api.admin.router` — so both ASGI entrypoints stay
about *process* concerns (pools, lifespan, probes) and adding a feature
is a line here, not there.

Unlike the console's table, no prefixes or tags are applied: these
modules predate the aggregate router and spell their full paths
(``/sessions/{session_id}/messages``) internally. Re-rooting them would
churn twelve modules to restate paths that have never needed to move, so
this table fixes only membership and order — and keeps the public
OpenAPI schema byte-identical to what it was before the table existed.
"""

from fastapi import APIRouter

from app.api.public import (
    auth,
    check_in,
    credits,
    feedback,
    mcp_server,
    meeting,
    meeting_library,
    project,
    schedule,
    session,
    share,
    sidebar_layout,
    skill,
    user,
)

public_router = APIRouter()

public_router.include_router(auth.router)
public_router.include_router(check_in.router)
public_router.include_router(credits.router)
public_router.include_router(feedback.router)
public_router.include_router(mcp_server.router)
public_router.include_router(meeting.router)
public_router.include_router(meeting_library.router)
public_router.include_router(project.router)
public_router.include_router(schedule.router)
public_router.include_router(session.router)
public_router.include_router(share.router)
public_router.include_router(sidebar_layout.router)
public_router.include_router(skill.router)
public_router.include_router(user.router)
