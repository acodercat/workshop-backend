"""Request / response models for the deployment's meeting AI settings."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.models.meeting_settings import MinutesSource


class MeetingSettingsChange(BaseModel):
    source: MinutesSource
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    # The revision the change was made against; a stale one is refused.
    revision: int = Field(ge=0)


class MeetingSettingsView(MeetingSettingsChange):
    # The models that may be chosen between.
    models: list[str]
