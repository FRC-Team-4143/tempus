"""
Timezone helpers and shared date/time utilities.

All datetimes in the database are stored as naive UTC.
These helpers convert to/from the configured local timezone (default: America/Chicago).
"""
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Chicago")


_UTC = ZoneInfo("UTC")


def utc_to_local(dt: datetime) -> datetime:
    """Convert a naive UTC datetime to a naive local datetime."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC).astimezone(_tz()).replace(tzinfo=None)


def local_to_utc(dt: datetime) -> datetime:
    """Convert a naive local datetime to a naive UTC datetime (for DB queries)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_tz()).astimezone(_UTC).replace(tzinfo=None)


def today_local() -> date:
    """Today's date in the local timezone."""
    return datetime.now(_tz()).date()


def effective_signout_utc(hhmm: str) -> Optional[datetime]:
    """Given an 'HH:MM' local time, return the most recent such moment as naive UTC.

    Used by the auto sign-out job to record forgotten sessions as ending at a
    fixed clock time rather than whenever the job happens to fire. Builds today's
    local date at HH:MM; if that is in the future (e.g. the job fired after
    midnight), rolls back one day so the result is always <= now. Returns None on
    malformed input so callers can fall back to the actual run time.
    """
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    now_local = datetime.now(_tz()).replace(tzinfo=None)
    effective_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if effective_local > now_local:
        effective_local -= timedelta(days=1)
    return local_to_utc(effective_local)


def format_elapsed(start: datetime, end: Optional[datetime] = None) -> str:
    """Format elapsed time between two naive UTC datetimes as 'Xh YYm'. end defaults to now."""
    end = end or datetime.utcnow()
    hours, rem = divmod(int((end - start).total_seconds()), 3600)
    return f"{hours}h {rem // 60:02d}m"


def current_week_bounds() -> tuple[datetime, datetime]:
    """Return (week_start_utc, week_end_utc) for the current Mon–Sun week."""
    week_start = today_local() - timedelta(days=today_local().weekday())
    week_end = week_start + timedelta(days=7)
    return (
        local_to_utc(datetime.combine(week_start, datetime.min.time())),
        local_to_utc(datetime.combine(week_end, datetime.min.time())),
    )
