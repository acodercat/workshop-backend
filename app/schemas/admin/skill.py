"""Request / response models for the console's skill surface.

One response shape for every origin: the console is the only surface
that sees all three (`SYSTEM` is hidden from users entirely), and a
platform skill differs from a user's only by `origin` and `owner`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.skill import SkillOrigin

PlatformSkillOrigin = Literal["SYSTEM", "OFFICIAL"]
"""The origins an operator may upload into.

``CUSTOM`` is absent, and not by omission: a ``CUSTOM`` skill belongs to
one user (``ck_skill_origin_user_id_consistent`` requires the owner), so
there is no such thing as an operator uploading one.
"""


class SkillResponse(BaseModel):
    """One skill as the console lists it.

    ``owner_username`` and ``owner_name`` are joined in for `CUSTOM`
    rows — the identity pair every console table renders together — and
    are ``None`` for the platform origins, which have no owner by
    construction (the ``ck_skill_origin_user_id_consistent`` check
    enforces that).

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
    # The tab this skill browses under, meaningful only on ``OFFICIAL``
    # rows. ``None`` on a ``CUSTOM`` upload, which lives behind the
    # pinned personal tab and is not the platform's to classify.
    category_id: int | None
    user_id: int | None
    owner_username: str | None
    owner_name: str | None
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
    caller having to resend what it is not changing —
    ``category_id`` excepted, below.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=1024)
    enabled: bool | None = None
    # The one field where an explicit ``null`` means "clear" rather than
    # "leave alone": retiring a category, or correcting a skill filed
    # under the wrong one, are ordinary operator moves that this
    # surface's general rule would leave no way to express.
    # ``update_skill`` reads ``model_fields_set`` to tell the two apart.
    category_id: int | None = None

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "SkillUpdate":
        """Refuse a request that asks for nothing.

        Every field is optional, so without this an empty body is a valid
        PATCH that the endpoint's no-op guard then answers 200 — a request
        that did nothing, reported as success.

        Keyed on ``model_fields_set`` — what the caller actually sent —
        rather than on the non-``None`` values, so ``{"category_id": null}``
        reads as the clearing it is instead of as an empty body. That is
        also what ``update_skill`` keys its own explicit-``null`` handling
        on, so the guard and the endpoint cannot disagree about what
        counts as a change.
        """
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to change.")
        return self
