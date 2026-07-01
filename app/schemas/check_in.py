"""Pydantic schemas for the daily check-in endpoints.

Credits on the wire are whole-credit integers, same as the credit
endpoints (the service works in µ-credits and converts at the boundary).
``streak_day`` is the consecutive-day count for the "已连续签到 N 天"
display; it does not affect the reward in this MVP.
"""

from pydantic import BaseModel


class CheckInStatusResponse(BaseModel):
    """Response of ``GET /check-ins/status`` — enough for the UI to render
    the button (enabled / already-claimed) and the streak.

    ``enabled`` gates the whole surface (off when the reward is 0);
    ``checked_in_today`` decides whether the claim button is live;
    ``reward_credits`` is what a claim grants today."""

    enabled: bool
    checked_in_today: bool
    streak_day: int
    total_days: int
    reward_credits: int


class CheckInResponse(BaseModel):
    """Result of ``POST /check-ins`` — the claimed reward, the new streak,
    and the fresh credit balance so the client refreshes in one round-trip."""

    reward_credits: int
    streak_day: int
    balance_credits: int
