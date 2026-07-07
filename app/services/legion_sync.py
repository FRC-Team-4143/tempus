"""
Legion roster sync — pulls the source-of-truth roster from Legion's read-only API and
upserts it into Tempus's local mirror (`Student`/`Mentor`/`Team`/`Subteam`).

Data flows one way: Legion → Tempus. Tempus never writes roster data back. Members are
keyed on Legion's stable `member_code`; existing local rows created before the cutover are
back-linked by `slack_user_id` (unique) then by exact name on first sync. Incremental
syncs pass `updated_since` (the previous sync's start time) so only changed members are
fetched. Group slugs travel on each member and drive lead status (see services/leads.py).
"""
import json
import logging
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Mentor, Student, Subteam, Team
from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting, set_setting

log = logging.getLogger(__name__)


class LegionSyncError(RuntimeError):
    """Raised when the sync can't run (misconfigured or Legion unreachable)."""


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    resp = await client.get(path, params={k: v for k, v in params.items() if v is not None})
    resp.raise_for_status()
    return resp.json()


async def sync_roster(db: AsyncSession, *, full: bool = False) -> str:
    """Pull teams, subteams, and members from Legion and upsert the local mirror.
    Pass `full=True` to ignore the incremental watermark and re-pull everyone.
    Returns a short human summary. Raises `LegionSyncError` on config/transport failure."""
    if not settings.legion_base_url or not settings.legion_api_key:
        raise LegionSyncError("Legion is not configured (set LEGION_BASE_URL and LEGION_API_KEY).")

    sync_start = datetime.utcnow().isoformat()
    since = None if full else await get_setting(db, LEGION_LAST_SYNCED_KEY)
    headers = {"X-API-Key": settings.legion_api_key}
    try:
        async with httpx.AsyncClient(
            base_url=settings.legion_base_url, headers=headers, timeout=30
        ) as client:
            teams = (await _get(client, "/api/teams"))["teams"]
            subteams = (await _get(client, "/api/subteams"))["subteams"]
            members = (await _get(client, "/api/members", updated_since=since))["members"]
    except (httpx.HTTPError, KeyError) as e:
        raise LegionSyncError(f"Legion API request failed: {e}") from e

    await _upsert_teams(db, teams)
    await _upsert_subteams(db, subteams)
    await db.flush()  # so team ids exist for member linking
    counts = await _upsert_members(db, members)

    # Watermark = this sync's start; a member changed mid-sync is re-pulled next time (>=).
    await set_setting(db, LEGION_LAST_SYNCED_KEY, sync_start)  # commits
    summary = (
        f"{counts['students']} students, {counts['mentors']} mentors, "
        f"{counts['skipped']} skipped, {len(teams)} teams, {len(subteams)} subteams"
    )
    log.info("Legion sync complete: %s (since=%s)", summary, since or "full")
    return summary


async def _upsert_teams(db: AsyncSession, teams: list[dict]) -> None:
    existing = {t.number: t for t in (await db.execute(select(Team))).scalars().all()}
    for t in teams:
        row = existing.get(t["number"])
        if row:
            row.name = t["name"]
        else:
            db.add(Team(number=t["number"], name=t["name"]))


async def _upsert_subteams(db: AsyncSession, subteams: list[dict]) -> None:
    existing = {s.slug: s for s in (await db.execute(select(Subteam))).scalars().all()}
    for order, s in enumerate(subteams):
        row = existing.get(s["slug"])
        if row:
            row.label = s["label"]
            row.is_active = s["is_active"]
            row.sort_order = order
        else:
            db.add(Subteam(
                slug=s["slug"], label=s["label"],
                is_active=s["is_active"], sort_order=order,
            ))


async def _find_local(db: AsyncSession, model, member: dict):
    """Locate the local row for a Legion member: by member_code, else back-link by
    slack_user_id, else by exact (case-insensitive) name. Returns the row or None."""
    code = member["member_code"]
    row = (await db.execute(select(model).where(model.member_code == code))).scalars().first()
    if row:
        return row
    slack_id = member.get("slack_user_id")
    if slack_id:
        row = (await db.execute(
            select(model).where(model.slack_user_id == slack_id)
        )).scalars().first()
        if row:
            return row
    return (await db.execute(
        select(model).where(func.lower(model.name) == member["name"].lower())
    )).scalars().first()


async def _upsert_members(db: AsyncSession, members: list[dict]) -> dict:
    team_by_number = {
        t.number: t.id for t in (await db.execute(select(Team))).scalars().all()
    }
    counts = {"students": 0, "mentors": 0, "skipped": 0}

    for m in members:
        is_student = m["role"] == "student"
        model = Student if is_student else Mentor
        team_id = team_by_number.get(m.get("team_number")) if m.get("team_number") else None
        subteam_slug = (m.get("subteam") or {}).get("slug")

        # A Student requires a team (team_id NOT NULL); skip + log if Legion has none.
        if is_student and team_id is None:
            log.warning("Skipping student %s (%s): no team in Legion", m["name"], m["member_code"])
            counts["skipped"] += 1
            continue

        row = await _find_local(db, model, m)
        if row is None:
            row = model(member_code=m["member_code"])
            db.add(row)
        row.member_code = m["member_code"]
        row.name = m["name"]
        row.team_id = team_id
        row.subteam_slug = subteam_slug
        # slack_user_id is NOT NULL on Mentor; default to "" if Legion has none.
        row.slack_user_id = m.get("slack_user_id") or ("" if not is_student else None)
        row.is_active = m["is_active"]
        row.archived_at = None if m["is_active"] else (row.archived_at or datetime.utcnow())
        if not is_student:
            row.group_slugs = json.dumps(m.get("groups") or [])

        counts["students" if is_student else "mentors"] += 1

    return counts
