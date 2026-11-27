"""The deployment's meeting AI settings — who writes minutes, and with
which model.

- ``GET /meeting-settings``
- ``PUT /meeting-settings`` — compare-and-swap on ``revision``.

One row for the whole deployment, so it is the operators' to change, not
any customer's.
"""

from fastapi import APIRouter

from app.api.admin.dependencies import StaffContext
from app.schemas.admin.meeting_settings import MeetingSettingsChange, MeetingSettingsView
from app.services.meeting import settings as meeting_settings

router = APIRouter(tags=["meeting-settings"])


def _view(current: meeting_settings.CurrentSettings) -> MeetingSettingsView:
    return MeetingSettingsView(
        source=current.source,
        model=current.model,
        revision=current.revision,
        models=meeting_settings.available_models(),
    )


@router.get("/meeting-settings")
async def get_meeting_settings(ctx: StaffContext) -> MeetingSettingsView:
    return _view(await meeting_settings.read(ctx.db))


@router.put("/meeting-settings")
async def change_meeting_settings(
    ctx: StaffContext, body: MeetingSettingsChange
) -> MeetingSettingsView:
    return _view(
        await meeting_settings.change(
            ctx.db, source=body.source, model=body.model, revision=body.revision
        )
    )
