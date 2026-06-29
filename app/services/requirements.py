"""
Weekly requirement resolution.

A requirement can be scoped from most-specific to least-specific:
  1. specific team + specific category
  2. specific team + all categories  (category is NULL)
  3. all teams    + specific category (team_id is NULL)
  4. all teams    + all categories    (both NULL)

Team is the primary filter, category secondary — the most specific match wins.
Within a scope, the most recent entry with week_start <= the target week is used.
Falls back to DEFAULT_REQUIRED_HOURS when nothing matches.
"""
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FocusCategory, WeeklyRequirement

DEFAULT_REQUIRED_HOURS = 11.0


def requirement_lookup_order(
    team_id: Optional[int], category: Optional[FocusCategory]
) -> list[tuple[Optional[int], Optional[FocusCategory]]]:
    """Most-specific-first list of (team_id, category) scope keys to try."""
    keys: list[tuple[Optional[int], Optional[FocusCategory]]] = []
    if category is not None:
        keys.append((team_id, category))
    keys.append((team_id, None))
    if team_id is not None and category is not None:
        keys.append((None, category))
    if team_id is not None:
        keys.append((None, None))
    return keys


async def resolve_requirement(
    db: AsyncSession,
    team_id: Optional[int],
    category: Optional[FocusCategory],
    week_start: date,
    default: float = DEFAULT_REQUIRED_HOURS,
) -> float:
    """Return the required hours for a team+category in a given week, most-specific scope first."""
    for tid, cat in requirement_lookup_order(team_id, category):
        team_clause = (
            WeeklyRequirement.team_id.is_(None) if tid is None
            else WeeklyRequirement.team_id == tid
        )
        cat_clause = (
            WeeklyRequirement.category.is_(None) if cat is None
            else WeeklyRequirement.category == cat
        )
        q = (
            select(WeeklyRequirement)
            .where(team_clause, cat_clause, WeeklyRequirement.week_start <= week_start)
            .order_by(WeeklyRequirement.week_start.desc())
            .limit(1)
        )
        r = (await db.execute(q)).scalars().first()
        if r:
            return r.required_hours
    return default
