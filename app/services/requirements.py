"""
Weekly requirement resolution.

A requirement can be scoped from most-specific to least-specific:
  1. specific team + specific subteam
  2. specific team + all subteams  (subteam_slug is NULL)
  3. all teams    + specific subteam (team_id is NULL)
  4. all teams    + all subteams    (both NULL)

Team is the primary filter, subteam secondary — the most specific match wins.
Within a scope, the most recent entry with week_start <= the target week is used.
Falls back to DEFAULT_REQUIRED_HOURS when nothing matches.
"""
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WeeklyRequirement

DEFAULT_REQUIRED_HOURS = 11.0


def requirement_lookup_order(
    team_id: Optional[int], subteam_slug: Optional[str]
) -> list[tuple[Optional[int], Optional[str]]]:
    """Most-specific-first list of (team_id, subteam_slug) scope keys to try."""
    keys: list[tuple[Optional[int], Optional[str]]] = []
    if subteam_slug is not None:
        keys.append((team_id, subteam_slug))
    keys.append((team_id, None))
    if team_id is not None and subteam_slug is not None:
        keys.append((None, subteam_slug))
    if team_id is not None:
        keys.append((None, None))
    return keys


async def resolve_requirement(
    db: AsyncSession,
    team_id: Optional[int],
    subteam_slug: Optional[str],
    week_start: date,
    default: float = DEFAULT_REQUIRED_HOURS,
) -> float:
    """Return the required hours for a team+subteam in a given week, most-specific scope first."""
    for tid, slug in requirement_lookup_order(team_id, subteam_slug):
        team_clause = (
            WeeklyRequirement.team_id.is_(None) if tid is None
            else WeeklyRequirement.team_id == tid
        )
        slug_clause = (
            WeeklyRequirement.subteam_slug.is_(None) if slug is None
            else WeeklyRequirement.subteam_slug == slug
        )
        q = (
            select(WeeklyRequirement)
            .where(team_clause, slug_clause, WeeklyRequirement.week_start <= week_start)
            .order_by(WeeklyRequirement.week_start.desc())
            .limit(1)
        )
        r = (await db.execute(q)).scalars().first()
        if r:
            return r.required_hours
    return default
