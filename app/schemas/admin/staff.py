"""Request / response models for operator accounts.

Two response shapes, deliberately not one:

- :class:`StaffProfileResponse` — the caller looking at *themselves*.
- :class:`StaffResponse` — the management view of *an account*.

The management view carries ``status`` and ``created_at``; the self view
does not, because the console renders its header from it and has no use
for either. Neither carries ``password_hash`` or ``token_version``.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.staff import StaffStatus
from app.schemas.admin.fields import Password


class StaffCreate(BaseModel):
    """A new operator account."""

    username: str = Field(
        min_length=3,
        max_length=64,
        # Login identifier, hand-provisioned, and it appears in log lines.
        # Kept to an unambiguous set so it can't be confused with a display
        # name or carry look-alike characters.
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    name: str = Field(min_length=1, max_length=64)
    password: Password


class StaffUpdate(BaseModel):
    """A partial change to an account. Absent fields are left alone.

    ``None`` is not a value here — every field is optional and omitting
    it means "don't touch". There is no column this can null out, so the
    ambiguity never arises.
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: StaffStatus | None = None

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "StaffUpdate":
        """Refuse a request that changes nothing.

        An empty PATCH is a client bug; answering 200 would hide it.
        """
        if self.name is None and self.status is None:
            raise ValueError("Provide at least one of name or status.")
        return self


class StaffPasswordReset(BaseModel):
    """An operator setting someone else's password.

    No ``current_password``: the caller doesn't know it, and requiring it
    would make the recovery case — an operator who is locked out — the
    one case this can't serve.
    """

    password: Password


class StaffPasswordChange(BaseModel):
    """An operator changing their own password.

    Sudo-gated on ``current_password``: possession of a live token is
    not on its own enough to rotate the credential behind it.
    """

    current_password: str = Field(min_length=1, max_length=128)
    new_password: Password


class StaffProfileResponse(BaseModel):
    """The signed-in operator, as the console renders its own header."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    last_login_at: datetime | None


class StaffResponse(BaseModel):
    """An operator account as the management surface lists it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    status: StaffStatus
    last_login_at: datetime | None
    created_at: datetime
