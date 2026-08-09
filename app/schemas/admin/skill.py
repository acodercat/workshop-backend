"""Request / response models for the console's skill surface.

One response shape for every origin: the console is the only surface
that sees all three (`SYSTEM` is hidden from users entirely), and a
platform skill differs from a user's only by `origin` and `owner`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.skill import SkillOrigin
from app.schemas.admin.fields import Reason

PlatformSkillOrigin = Literal["SYSTEM", "OFFICIAL"]
"""The origins an operator may upload into.

``CUSTOM`` is absent, and not by omission: a ``CUSTOM`` skill belongs to
one user (``ck_skill_origin_user_id_consistent`` requires the owner), so
there is no such thing as an operator uploading one.
"""


class SkillResponse(BaseModel):
    """One skill as the console lists it.

    ``owner_username`` is joined in for `CUSTOM` rows and ``None`` for
    the platform origins, which have no owner by construction (the
    ``ck_skill_origin_user_id_consistent`` check enforces that).

    ``enabled`` here is the **platform default**, not what any given
    user experiences: ``user_skill_preference`` overrides it per user,
    and the effective value is ``COALESCE(pref.enabled, skill.enabled)``.
    """

    id: int
    origin: SkillOrigin
    name: str
    display_name: str
    description: str
    enabled: bool
    bundle_size: int
    user_id: int | None
    owner_username: str | None
    created_at: datetime
    updated_at: datetime


class SkillUpdate(BaseModel):
    """Edit a skill's picker metadata, or flip its platform-wide default.

    The same three fields the public surface calls editable, for the
    same reason: ``display_name`` and ``description`` are what the
    picker shows, not what the bundle *does*. A user may edit them on a
    skill they own and is refused on a platform one — "they'd need to
    mutate the shared row" — and this is the surface that owns that row.

    ``name`` stays immutable, as it is there: it is derived from the
    bundle's ``SKILL.md`` and is how the worker finds the staged
    directory, so it changes by re-uploading and no other way. The
    bundle's contents are likewise immutable on this path.

    Every field is optional and a ``None`` is "leave alone", so one
    endpoint serves a rename, a re-description and the toggle without a
    caller having to resend what it is not changing.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=1024)
    enabled: bool | None = None
    reason: Reason
