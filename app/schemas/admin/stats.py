"""Response models for the console dashboard.

Money on the wire is **whole credits** (int), same as every other
console surface; µ-credits stay an internal precision detail and the
conversion happens at the endpoint.
"""

from datetime import date, datetime

from pydantic import BaseModel


class DailySpendPoint(BaseModel):
    """One business day of the spend series. Quiet days are present with
    zero rather than omitted, so the chart's x-axis is contiguous."""

    day: date
    spend_credits: int


class ModelSpendEntry(BaseModel):
    """Consumption attributed to one model over the window."""

    model_id: str
    spend_credits: int


class CreditsStatsResponse(BaseModel):
    """The dashboard's tiles, chart series and model split for one
    window.

    ``spend`` counts settled turn charges only — see
    :mod:`app.services.credits.analytics` for why that is the honest
    definition. ``outstanding_credits`` is the one figure that is *not*
    windowed: it is the prepaid liability across all users right now.
    """

    days: int
    spend_credits: int
    granted_credits: int
    granted_free_credits: int
    clawed_back_credits: int
    active_users: int
    settled_turns: int
    outstanding_credits: int
    daily: list[DailySpendPoint]
    by_model: list[ModelSpendEntry]


class UserActivityEntry(BaseModel):
    """One user's consumption over the window — the row shape behind
    both the spend leaderboard and the recently-active list."""

    user_id: int
    username: str
    name: str
    spend_credits: int
    settled_turns: int
    balance_credits: int
    last_spend_at: datetime


class CheckInStatsResponse(BaseModel):
    """Daily-check-in engagement for the window.

    Counted from the ``check_in`` table rather than its ``CHECKIN``
    ledger rows: the reward is configurable and could be switched off
    without the habit changing, so the habit gets its own source of
    truth.
    """

    days: int
    check_ins: int
    users: int


class CheckInActivityEntry(BaseModel):
    """One user's check-in habit over the window.

    ``streak_day`` comes from the user's most recent check-in — a running
    count the claim computes — so it reads as the streak they are *on*,
    not the best one they managed inside the window.
    """

    user_id: int
    username: str
    name: str
    check_ins: int
    streak_day: int
    last_check_in_at: datetime
