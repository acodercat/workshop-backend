"""What "today" means on this platform.

Every user-visible day boundary — the daily check-in, the console's
dashboard buckets, revenue reporting — has to agree, or the same event
lands on different days depending on which feature is asking. That day
is cut in ``settings.checkin_timezone`` (Asia/Shanghai), not UTC, so it
matches what operators and users mean by it.

The setting keeps its check-in name because it is the one already
deployed and renaming a live config key is not worth the churn; what
matters is that this module is the only place it is read.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import settings


def business_tz() -> ZoneInfo:
    """The timezone every day boundary is cut in."""
    return ZoneInfo(settings.checkin_timezone)


def business_today() -> date:
    """Today's business date."""
    return datetime.now(business_tz()).date()


def window_start(days: int) -> datetime:
    """UTC instant ``days`` business days back, aligned to the start of
    that day.

    A 7-day window is six whole previous days plus today — not a rolling
    168 hours, which would cut the earliest day in half and make the
    first bar of a chart lie.
    """
    local_midnight = datetime.now(business_tz()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (local_midnight - timedelta(days=days - 1)).astimezone(timezone.utc)


def business_days(days: int) -> list[date]:
    """The window's dates, oldest first — the x-axis a daily series is
    gap-filled against, so a quiet day reads as zero rather than a hole.
    """
    today = business_today()
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
