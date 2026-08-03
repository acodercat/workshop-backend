"""Request / response models for operator accounts.

Two response shapes, deliberately not one:

- :class:`StaffProfileResponse` — the caller looking at *themselves*.
- :class:`StaffResponse` — an owner looking at *an account*.

The management view carries ``status`` and ``created_at``; the self view
does not, because the console renders its header from it and has no use
for either. Neither carries ``password_hash`` or ``token_version``.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.staff import StaffRole, StaffStatus
from app.services.admin.staff import MIN_PASSWORD_CHARS

Password = Annotated[str, Field(min_length=MIN_PASSWORD_CHARS, max_length=128)]
"""Shared password constraint, as an ``Annotated`` alias — the idiomatic
way to reuse a constraint across models (a shared ``Field()`` *instance*
would lean on pydantic internals). The floor comes from the service
module so the API, the provisioning script and the service cannot
disagree."""

Reason = Annotated[str, Field(min_length=1, max_length=500)]
"""Required justification. Changing an operator's role or disabling their
account is a privilege change, and the audit trail is worth little if
half its rows explain nothing."""


class StaffCreate(BaseModel):
    """A new operator account.

    ``role`` defaults to ``VIEWER``: least privilege unless the caller
    says otherwise, matching the column's server default.

    No ``reason`` — for a create, the payload *is* the record of what was
    done and why it was possible to describe.
    """

    username: str = Field(
        min_length=3,
        max_length=64,
        # Login identifier, hand-provisioned, and it appears in log lines
        # and audit rows. Kept to an unambiguous set so it can't be
        # confused with a display name or carry look-alike characters.
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    display_name: str = Field(min_length=1, max_length=64)
    role: StaffRole = StaffRole.VIEWER
    password: Password


class StaffUpdate(BaseModel):
    """A partial change to an account. Absent fields are left alone.

    ``None`` is not a value here — every field is optional and omitting
    it means "don't touch". There is no column this can null out, so the
    ambiguity never arises.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    role: StaffRole | None = None
    status: StaffStatus | None = None
    reason: Reason

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "StaffUpdate":
        """Refuse a request that changes nothing.

        An empty PATCH would otherwise write an audit row describing no
        change, which is noise in exactly the place noise is expensive.
        """
        if self.display_name is None and self.role is None and self.status is None:
            raise ValueError(
                "Provide at least one of display_name, role or status."
            )
        return self


class StaffPasswordReset(BaseModel):
    """An owner setting someone else's password.

    No ``current_password``: the owner doesn't know it, and requiring it
    would make the recovery case — an operator who is locked out — the
    one case this can't serve.
    """

    password: Password
    reason: Reason


class StaffPasswordChange(BaseModel):
    """An operator changing their own password.

    Sudo-gated on ``current_password``, mirroring the customer-facing
    email/phone change flows: possession of a live token is not on its
    own enough to rotate the credential behind it.
    """

    current_password: str = Field(min_length=1, max_length=128)
    new_password: Password


class StaffProfileResponse(BaseModel):
    """The signed-in operator, as the console renders its own header.

    The console gates its navigation on ``role``, but the server
    re-checks the role on every request — so this field is a UI
    convenience and never the enforcement point.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: StaffRole
    last_login_at: datetime | None


class StaffResponse(BaseModel):
    """An operator account as the management surface lists it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: StaffRole
    status: StaffStatus
    last_login_at: datetime | None
    created_at: datetime
