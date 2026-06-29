"""
Runtime-configurable app settings backed by the `app_settings` key/value table.

Single source of truth for the leaderboard "counts since" cutoff — the date from
which cumulative/all-time leaderboard totals are counted. A missing/blank value
means count all-time (the original behavior).
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.utils import local_to_utc

LEADERBOARD_SINCE_KEY = "leaderboard_since"


async def get_setting(db: AsyncSession, key: str) -> Optional[str]:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    return row.value if row else None


async def set_setting(db: AsyncSession, key: str, value: Optional[str]) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


async def get_leaderboard_since(db: AsyncSession) -> Optional[date]:
    """Date the leaderboard counts hours from, or None for 'all time'."""
    raw = await get_setting(db, LEADERBOARD_SINCE_KEY)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def set_leaderboard_since(db: AsyncSession, value: Optional[date]) -> None:
    """Upsert the leaderboard_since row. None clears it (back to all-time)."""
    await set_setting(db, LEADERBOARD_SINCE_KEY, value.isoformat() if value else None)


async def leaderboard_since_utc(db: AsyncSession) -> Optional[datetime]:
    """The cutoff as a naive-UTC datetime for `sign_in_time` comparisons, or None."""
    d = await get_leaderboard_since(db)
    if d is None:
        return None
    return local_to_utc(datetime.combine(d, datetime.min.time()))
