"""The caller's sidebar arrangement: ``/me/sidebar-layout``.

One resource per user, read and replaced whole, since dragging an item
reorders the list it is in (``app.models.sidebar_layout``). Two tabs saving
at once settle on whichever saved last; what that can lose is a position,
never a pin, which lives on the project or session itself.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.auth import authenticate_and_get_user
from app.models.sidebar_layout import SidebarLayout
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.sidebar_layout import SidebarLayoutResponse, SidebarLayoutUpdate

router = APIRouter()


@router.get("/me/sidebar-layout")
async def get_sidebar_layout(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SidebarLayoutResponse:
    """The caller's arrangement; empty lists until their first save."""
    layout = await db.get(SidebarLayout, current_user.id)
    if layout is None:
        return SidebarLayoutResponse()
    return SidebarLayoutResponse.model_validate(layout)


@router.put("/me/sidebar-layout")
@limiter.limit("60/minute")
async def replace_sidebar_layout(
    request: Request,
    body: SidebarLayoutUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authenticate_and_get_user),
) -> SidebarLayoutResponse:
    """Replace the caller's arrangement with ``body``."""
    values = body.model_dump(mode="json")
    await db.execute(
        pg_insert(SidebarLayout)
        .values(user_id=current_user.id, **values)
        .on_conflict_do_update(
            index_elements=[SidebarLayout.user_id],
            set_={**values, "updated_at": func.now()},
        )
    )
    await db.commit()
    return SidebarLayoutResponse(pin_order=body.pin_order, project_order=body.project_order)
