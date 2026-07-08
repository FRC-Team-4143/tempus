"""
Weekly attendance report — per-student, per-week hours vs. requirements.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import or_

from app.models import AttendanceSession, Mentor, MentorSession, Student, WeeklyRequirement
from app.services.requirements import DEFAULT_REQUIRED_HOURS, requirement_lookup_order
from app.utils import local_to_utc, today_local, utc_to_local


def week_starts_in_range(date_from: date, date_to: date) -> list[date]:
    """Return each Monday between date_from and date_to (both snapped to their Monday), capped at 26."""
    start = date_from - timedelta(days=date_from.weekday())
    end = date_to - timedelta(days=date_to.weekday())
    weeks: list[date] = []
    cur = start
    while cur <= end and len(weeks) < 26:
        weeks.append(cur)
        cur += timedelta(days=7)
    return weeks


def default_report_range(since: Optional[date]) -> tuple[date, date]:
    """The report view's default (date_from, date_to) when the caller hasn't filtered:
    from the Monday of the "counts hours since" week (app_settings.get_leaderboard_since)
    through the current week — falling back to a rolling 4-week window when no cutoff is
    configured, matching the app's original behavior."""
    today = today_local()
    this_monday = today - timedelta(days=today.weekday())
    if since is not None:
        start = since - timedelta(days=since.weekday())
    else:
        start = this_monday - timedelta(weeks=3)
    return start, this_monday


async def weekly_attendance_report(
    db: AsyncSession,
    week_starts: list[date],
    team_id: Optional[int] = None,
    subteam_slug: Optional[str] = None,
    student_ids: Optional[list[int]] = None,
) -> list[dict]:
    """
    Returns one dict per student, sorted by team number then name:
      {
        "student": Student,
        "weeks": [{"week_start": date, "hours": float, "required": float, "met": bool}, ...],
        "total_hours": float,
        "weeks_met": int,
        "weeks_total": int,
      }

    `student_ids`, when given, scopes to just those students (e.g. a personal report
    view for one student) — independent of / combinable with `team_id`/`subteam_slug`.
    """
    if not week_starts:
        return []

    range_start = week_starts[0]
    range_end = week_starts[-1] + timedelta(days=7)
    range_start_utc = local_to_utc(datetime.combine(range_start, datetime.min.time()))
    range_end_utc = local_to_utc(datetime.combine(range_end, datetime.min.time()))

    # Load students with team
    student_q = (
        select(Student)
        .options(selectinload(Student.team))
        .order_by(Student.team_id, Student.name)
    )
    if team_id is not None:
        student_q = student_q.where(Student.team_id == team_id)
    if subteam_slug is not None:
        student_q = student_q.where(Student.subteam_slug == subteam_slug)
    if student_ids is not None:
        student_q = student_q.where(Student.id.in_(student_ids))
    students = (await db.execute(student_q)).scalars().all()

    if not students:
        return []

    student_ids = [s.id for s in students]
    team_ids = list({s.team_id for s in students})

    # One query for all completed sessions in range
    sessions = (
        await db.execute(
            select(AttendanceSession).where(
                AttendanceSession.student_id.in_(student_ids),
                AttendanceSession.sign_out_time.is_not(None),
                AttendanceSession.sign_in_time >= range_start_utc,
                AttendanceSession.sign_in_time < range_end_utc,
            )
        )
    ).scalars().all()

    # Bucket hours by (student_id, monday)
    hours_map: dict[tuple[int, date], float] = {}
    for s in sessions:
        local_date = utc_to_local(s.sign_in_time).date()
        monday = local_date - timedelta(days=local_date.weekday())
        key = (s.student_id, monday)
        hours_map[key] = hours_map.get(key, 0.0) + (s.hours_counted or 0.0)

    # One query for all WeeklyRequirement rows that could apply — including all-teams (NULL team_id)
    req_q = select(WeeklyRequirement).where(
        or_(WeeklyRequirement.team_id.in_(team_ids), WeeklyRequirement.team_id.is_(None)),
        WeeklyRequirement.week_start <= range_end,
    )
    all_reqs = (await db.execute(req_q)).scalars().all()

    # Group by (team_id, subteam_slug), descending week so fallback lookup is an O(n) scan
    req_by_team_sub: dict[tuple, list] = defaultdict(list)
    for r in sorted(all_reqs, key=lambda x: x.week_start, reverse=True):
        req_by_team_sub[(r.team_id, r.subteam_slug)].append(r)

    def _resolve_req(tid: int, slug: Optional[str], week: date) -> float:
        # Most-specific scope first (team+subteam, team, all-teams+subteam, all-teams)
        for k_tid, k_slug in requirement_lookup_order(tid, slug):
            for r in req_by_team_sub.get((k_tid, k_slug), []):
                if r.week_start <= week:
                    return r.required_hours
        return DEFAULT_REQUIRED_HOURS

    rows = []
    for student in students:
        week_rows = []
        total_hours = 0.0
        weeks_met = 0
        for ws in week_starts:
            hours = round(hours_map.get((student.id, ws), 0.0), 2)
            required = _resolve_req(student.team_id, student.subteam_slug, ws)
            met = hours >= required
            week_rows.append({"week_start": ws, "hours": hours, "required": required, "met": met})
            total_hours += hours
            if met:
                weeks_met += 1

        rows.append({
            "student": student,
            "weeks": week_rows,
            "total_hours": round(total_hours, 2),
            "weeks_met": weeks_met,
            "weeks_total": len(week_starts),
        })

    return rows


async def weekly_mentor_hours(db: AsyncSession, week_starts: list[date], mentor_id: int) -> Optional[dict]:
    """
    The mentor analogue of `weekly_attendance_report`, for one mentor. Simpler than the
    student version — mentor hours have no weekly requirement or status multiplier
    (`MentorSession` has no `status` column; hours are always counted in full), so this
    just buckets `hours_counted` by week:
      {
        "mentor": Mentor,
        "weeks": [{"week_start": date, "hours": float}, ...],
        "total_hours": float,
      }
    Returns None if `mentor_id` doesn't match an existing mentor.
    """
    mentor = (await db.execute(select(Mentor).where(Mentor.id == mentor_id))).scalars().first()
    if mentor is None:
        return None
    if not week_starts:
        return {"mentor": mentor, "weeks": [], "total_hours": 0.0}

    range_start = week_starts[0]
    range_end = week_starts[-1] + timedelta(days=7)
    range_start_utc = local_to_utc(datetime.combine(range_start, datetime.min.time()))
    range_end_utc = local_to_utc(datetime.combine(range_end, datetime.min.time()))

    sessions = (
        await db.execute(
            select(MentorSession).where(
                MentorSession.mentor_id == mentor_id,
                MentorSession.sign_out_time.is_not(None),
                MentorSession.sign_in_time >= range_start_utc,
                MentorSession.sign_in_time < range_end_utc,
            )
        )
    ).scalars().all()

    hours_map: dict[date, float] = {}
    for s in sessions:
        local_date = utc_to_local(s.sign_in_time).date()
        monday = local_date - timedelta(days=local_date.weekday())
        hours_map[monday] = hours_map.get(monday, 0.0) + (s.hours_counted or 0.0)

    week_rows = []
    total_hours = 0.0
    for ws in week_starts:
        hours = round(hours_map.get(ws, 0.0), 2)
        week_rows.append({"week_start": ws, "hours": hours})
        total_hours += hours

    return {"mentor": mentor, "weeks": week_rows, "total_hours": round(total_hours, 2)}
