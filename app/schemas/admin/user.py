"""Request / response models for the console's user directory and the
per-user credit surface.

Money on the wire is **whole credits** (int, signed where applicable) —
the same convention as the customer credit endpoints; µ-credits stay an
internal precision detail and conversion happens at the endpoint.

Transaction rows reuse :class:`app.schemas.credits.CreditsTransactionEntry`
directly: support reads the same collapsed, user-facing view the customer
sees, because that is the view a customer disputes.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.user import UserStatus


class UserResponse(BaseModel):
    """One row of the user directory.

    ``balance_credits`` rides along because ``user.credit_balance`` is a
    column on the row — showing it costs no extra query, and "who is
    this / what's their balance" is the single most common support pair.
    """

    id: int
    username: str
    name: str
    email: str | None
    phone: str | None
    status: UserStatus
    balance_credits: int
    created_at: datetime


class UserDetailResponse(UserResponse):
    """The directory row plus what a support conversation needs next."""

    last_login_at: datetime | None
    session_count: int


class UserUpdate(BaseModel):
    """Change a user's account status — the only field the console may write."""

    status: UserStatus


class CreditAdjustmentCreate(BaseModel):
    """A manual signed correction to a user's balance.

    ``idempotency_key`` is minted by the console frontend once per form
    submission, so a retried request cannot apply twice — the ledger's
    UNIQUE constraint is the enforcement, this field just carries it.
    """

    amount_credits: int = Field(
        ...,
        ge=-1_000_000,
        le=1_000_000,
        description="Signed whole credits; positive grants, negative claws back.",
    )
    idempotency_key: str = Field(min_length=8, max_length=64)

    @field_validator("amount_credits")
    @classmethod
    def _non_zero(cls, value: int) -> int:
        """A zero adjustment would be a no-op ledger row that only adds
        audit noise — the service rejects it too; failing here makes it
        a 422 instead of a 500."""
        if value == 0:
            raise ValueError("Adjustment amount must be non-zero.")
        return value


class CreditAdjustmentResponse(BaseModel):
    """The applied correction: the ledger row it created and the balance
    it produced."""

    ledger_id: int
    amount_credits: int
    balance_credits: int
