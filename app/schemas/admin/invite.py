"""Request / response models for invite codes and their redemptions."""

from datetime import datetime

from pydantic import BaseModel

from app.models.invite_code import InviteCodeOrigin


class InviteCodeResponse(BaseModel):
    """One invite code.

    ``max_uses`` is ``None`` for unlimited — the column's own meaning,
    kept rather than flattened to a sentinel number.
    """

    id: int
    code: str
    origin: InviteCodeOrigin
    owner_user_id: int | None
    owner_username: str | None
    max_uses: int | None
    used_count: int
    enabled: bool
    note: str | None
    expires_at: datetime | None
    created_at: datetime


class InviteRedemptionResponse(BaseModel):
    """One redemption — who joined on whose code.

    ``referrer_*`` is ``None`` for ``SYSTEM`` codes: nobody referred them.
    """

    id: int
    code: str
    origin: InviteCodeOrigin
    invitee_user_id: int
    invitee_username: str
    referrer_user_id: int | None
    referrer_username: str | None
    created_at: datetime
