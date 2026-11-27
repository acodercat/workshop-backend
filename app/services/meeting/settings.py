"""The deployment's meeting AI settings: read by the meeting workspace,
changed by operators in the admin console."""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import MeetingChangedError, MeetingSettingsInvalidError
from app.models.meeting_settings import MeetingSettings, MinutesSource
from config import settings


def available_models() -> list[str]:
    """The models an operator may choose, in configured order."""
    return list(settings.meeting_models) or [settings.llm_model]


@dataclass(frozen=True)
class CurrentSettings:
    source: MinutesSource
    model: str
    revision: int


async def read(db: AsyncSession) -> CurrentSettings:
    row = (await db.execute(select(MeetingSettings).where(MeetingSettings.id == 1))).scalar_one()
    models = available_models()
    # A model removed from configuration since it was chosen falls back to
    # the default rather than being requested and failing every time.
    model = row.model if row.model in models else models[0]
    return CurrentSettings(source=row.source, model=model, revision=row.revision)


async def change(
    db: AsyncSession, *, source: MinutesSource, model: str, revision: int
) -> CurrentSettings:
    """Save new settings if nobody has since ``revision``. Commits."""
    if model not in available_models():
        raise MeetingSettingsInvalidError("That model is not available.")
    changed = (
        await db.execute(
            update(MeetingSettings)
            .where(MeetingSettings.id == 1, MeetingSettings.revision == revision)
            .values(source=source, model=model, revision=MeetingSettings.revision + 1)
            .returning(MeetingSettings.revision)
        )
    ).scalar_one_or_none()
    if changed is None:
        raise MeetingChangedError("The settings changed since they were read.")
    await db.commit()
    return CurrentSettings(source=source, model=model, revision=changed)
