"""Deployment-wide choices for how meetings use AI. One row.

``source`` decides who writes and translates minutes:

- ``meeting`` — the Meeting service only; if it cannot, the request fails.
- ``system`` — this deployment's own model only.
- ``auto`` — Meeting first, the system model when Meeting cannot.

``model`` is the system model used whenever one is, chosen from
``settings.meeting_models``. ``revision`` makes an edit a compare-and-swap,
so two administrators saving at once cannot silently overwrite each other.
"""

from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum as SAEnum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MinutesSource(StrEnum):
    AUTO = "auto"
    MEETING = "meeting"
    SYSTEM = "system"


class MeetingSettings(Base):
    __tablename__ = "meeting_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[MinutesSource] = mapped_column(
        SAEnum(
            MinutesSource,
            name="minutes_source",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    # Empty means "the first of ``settings.meeting_models``", so a deployment
    # that never visits the settings page follows its configuration.
    model: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (CheckConstraint("id = 1", name="ck_meeting_settings_singleton"),)
