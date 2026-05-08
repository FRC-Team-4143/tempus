"""
Kiosk routes — sign-in page, badge POST, SSE stream, and leaderboard stats.
"""
import ipaddress
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AttendanceSession, MentorSession, Mentor, Student, Team
from app.schemas import SignInRequest, SignInResponse
from app.services.attendance import (
    sign_in, get_signed_in_students,
    mentor_sign_in, get_signed_in_mentors, mentor_sign_out_all_open,
)
from app.services.broadcaster import broadcaster
from app.utils import utc_to_local, today_local, local_to_utc

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)


def _is_allowed_ip(request: Request) -> bool:
    """Return True if IP whitelisting is disabled or the client IP is in an allowed CIDR."""
    whitelist_str = settings.signin_ip_whitelist.strip()
    if not whitelist_str:
        return True
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        addr = ipaddress.ip_address(client_ip)
        for cidr in whitelist_str.split(","):
            if addr in ipaddress.ip_network(cidr.strip(), strict=False):
                return True
    except ValueError:
        pass
    return False


def _format_sessions(sessions) -> dict:
    """Return signed-in students grouped by team number."""
    by_team: dict[int, list[dict]] = {}
    for s in sessions:
        team_number = s.student.team.number
        if team_number not in by_team:
            by_team[team_number] = []
        elapsed = datetime.utcnow() - s.sign_in_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes = remainder // 60
        by_team[team_number].append(
            {
                "session_id": s.id,
                "name": s.student.name,
                "sign_in_time": utc_to_local(s.sign_in_time).strftime("%I:%M %p"),
                "elapsed": f"{hours}h {minutes:02d}m",
            }
        )
    return by_team


@router.get("/kiosk", response_class=HTMLResponse)
async def kiosk_page(request: Request, db: AsyncSession = Depends(get_db)):
    sessions = await get_signed_in_students(db)
    by_team = _format_sessions(sessions)
    return templates.TemplateResponse(
        "kiosk.html",
        {
            "request": request,
            "by_team": by_team,
            "teams": [4143, 4423],
        },
    )


@router.get("/kiosk/demo", response_class=HTMLResponse)
async def kiosk_demo(request: Request):
    """Render the kiosk with fake students and stats to preview layout at scale."""
    import random
    names_4143 = [
        "Alex Johnson", "Brianna Smith", "Carlos Rivera", "Diana Chen",
        "Ethan Park", "Fiona Walsh", "George Okafor", "Hannah Kim",
        "Isaac Patel", "Julia Martinez", "Kevin Nguyen", "Lily Thompson",
        "Marcus Davis", "Naomi Wilson", "Owen Scott", "Penelope Cruz",
        "Quincy Adams", "Rachel Green", "Samuel Torres", "Tasha Reeves",
        "Ulrich Baxter", "Vanessa Holt", "Wesley Ford", "Xiomara Ruiz",
        "Yusuf Ahmed",
    ]
    names_4423 = [
        "Priya Sharma", "Quinn Baker", "Rafael Torres", "Sophia Lee",
        "Tyler Brown",
    ]
    all_names = names_4143 + names_4423

    def _fake_students(names):
        entries = []
        base_hour = random.randint(7, 9)
        base_min = random.randint(0, 59)
        for i, name in enumerate(names):
            h = base_hour + (i * 4 + random.randint(0, 3)) // 60
            m = (base_min + i * 4 + random.randint(0, 3)) % 60
            elapsed_m = random.randint(5, 180)
            entries.append({
                "name": name,
                "sign_in_time": f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}",
                "elapsed": f"{elapsed_m // 60}h {elapsed_m % 60:02d}m",
            })
        return entries

    def _pick3(pool, value_fn, sort_key):
        sample = random.sample(pool, min(3, len(pool)))
        entries = [{"name": n, "value": value_fn()} for n in sample]
        entries.sort(key=lambda x: sort_key(x["value"]), reverse=True)
        return entries

    demo_stats = {
        "alltime": _pick3(all_names, lambda: f"{random.randint(10, 120)}.{random.randint(0,9)}h", lambda v: float(v[:-1])),
        "week":    _pick3(all_names, lambda: f"{random.randint(2, 18)}.{random.randint(0,9)}h",  lambda v: float(v[:-1])),
        "longest_session": _pick3(all_names, lambda: f"{random.randint(3, 10)}.{random.randint(0,9)}h", lambda v: float(v[:-1])),
        "streak":  _pick3(all_names, lambda: f"{random.randint(3, 21)}d", lambda v: int(v[:-1])),
        "team_totals": [
            {"name": "Team 4143", "value": f"{random.randint(300, 600) + round(random.random(), 1):.1f}h"},
            {"name": "Team 4423", "value": f"{random.randint(80, 200) + round(random.random(), 1):.1f}h"},
        ],
    }
    demo_stats["team_totals"].append({
        "name": "Combined",
        "value": f"{sum(float(r['value'][:-1]) for r in demo_stats['team_totals']):.1f}h",
    })

    by_team = {
        4143: _fake_students(names_4143),
        4423: _fake_students(names_4423),
    }
    return templates.TemplateResponse(
        "kiosk.html",
        {
            "request": request,
            "by_team": by_team,
            "teams": [4143, 4423],
            "demo": True,
            "demo_stats": demo_stats,
        },
    )


@router.post("/kiosk/signin", response_model=SignInResponse)
async def kiosk_signin(
    body: SignInRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    if not _is_allowed_ip(request):
        return SignInResponse(success=False, message="Sign-in not allowed from this location.")

    success, message, student = await sign_in(db, body.name.strip())
    if success:
        await broadcaster.broadcast("update")
        return SignInResponse(
            success=success,
            message=message,
            student_name=student.name if student else None,
            team_name=student.team.name if student else None,
        )

    # If student not found, try mentor (silently — mentors don't appear on student board)
    m_success, m_message, mentor = await mentor_sign_in(db, body.name.strip())
    if m_success:
        await broadcaster.broadcast("mentor_update")
        return SignInResponse(success=True, message=m_message)

    return SignInResponse(success=success, message=message)


@router.get("/kiosk/data")
async def kiosk_data(db: AsyncSession = Depends(get_db)):
    """JSON snapshot of currently signed-in students, grouped by team number."""
    sessions = await get_signed_in_students(db)
    by_team = _format_sessions(sessions)
    # Ensure both teams are always present in the response
    for t in [4143, 4423]:
        by_team.setdefault(t, [])
    return by_team


@router.get("/kiosk/stats")
async def kiosk_stats(db: AsyncSession = Depends(get_db)):
    """Return leaderboard stats for the kiosk stats panel."""

    # ── 1. All-time top hours ─────────────────────────────────────────────────
    alltime_result = await db.execute(
        select(Student.name, func.sum(AttendanceSession.hours_counted).label("total"))
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .group_by(Student.id)
        .order_by(func.sum(AttendanceSession.hours_counted).desc())
        .limit(3)
    )
    alltime = [{"name": r.name, "value": f"{r.total:.1f}h"} for r in alltime_result]

    # ── 2. This week top hours (Mon–Sun, CST) ─────────────────────────────────
    week_start = today_local() - timedelta(days=today_local().weekday())
    week_start_utc = local_to_utc(datetime.combine(week_start, datetime.min.time()))
    week_result = await db.execute(
        select(Student.name, func.sum(AttendanceSession.hours_counted).label("total"))
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(
            AttendanceSession.sign_in_time >= week_start_utc
        )
        .group_by(Student.id)
        .order_by(func.sum(AttendanceSession.hours_counted).desc())
        .limit(3)
    )
    week = [{"name": r.name, "value": f"{r.total:.1f}h"} for r in week_result]

    # ── 3. Longest single session ─────────────────────────────────────────────
    longest_result = await db.execute(
        select(Student.name, func.max(AttendanceSession.hours_counted).label("max_h"))
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .group_by(Student.id)
        .order_by(func.max(AttendanceSession.hours_counted).desc())
        .limit(3)
    )
    longest = [{"name": r.name, "value": f"{r.max_h:.1f}h"} for r in longest_result]

    # ── 4. Longest streak (consecutive days with >= 1 h) ──────────────────────
    streak_rows = (
        await db.execute(
            select(
                Student.id,
                Student.name,
                AttendanceSession.sign_in_time,
                AttendanceSession.hours_counted,
            )
            .join(Student, AttendanceSession.student_id == Student.id)
            .where(AttendanceSession.hours_counted.isnot(None))
            .where(AttendanceSession.sign_out_time.isnot(None))
        )
    ).all()

    # Sum hours per (student, calendar day)
    student_daily: dict[int, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    student_names: dict[int, str] = {}
    for row in streak_rows:
        student_names[row.id] = row.name
        student_daily[row.id][utc_to_local(row.sign_in_time).date()] += row.hours_counted or 0.0

    streaks: list[tuple[int, str]] = []
    for sid, daily in student_daily.items():
        qualifying = sorted(d for d, h in daily.items() if h >= 1.0)
        if not qualifying:
            continue
        best = current = 1
        for i in range(1, len(qualifying)):
            if (qualifying[i] - qualifying[i - 1]).days == 1:
                current += 1
                best = max(best, current)
            else:
                current = 1
        streaks.append((best, student_names[sid]))

    streaks.sort(reverse=True)
    streak = [{"name": name, "value": f"{s}d"} for s, name in streaks[:3]]

    # ── 5. Team totals (students only) ───────────────────────────────────────
    team_totals_result = await db.execute(
        select(Team.number, func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0).label("total"))
        .join(Student, Student.team_id == Team.id)
        .join(AttendanceSession, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(Team.number.in_([4143, 4423]))
        .group_by(Team.number)
        .order_by(Team.number)
    )
    team_rows = {r.number: r.total for r in team_totals_result}
    t4143 = team_rows.get(4143, 0.0)
    t4423 = team_rows.get(4423, 0.0)
    team_totals = [
        {"name": "Team 4143", "value": f"{t4143:.1f}h"},
        {"name": "Team 4423", "value": f"{t4423:.1f}h"},
        {"name": "Combined",  "value": f"{t4143 + t4423:.1f}h"},
    ]

    return {
        "alltime": alltime,
        "week": week,
        "longest_session": longest,
        "streak": streak,
        "team_totals": team_totals,
    }


@router.get("/kiosk/stream")
async def kiosk_stream():
    """Server-Sent Events endpoint — pushes 'update' events to all connected kiosks."""

    async def event_generator() -> AsyncGenerator[str, None]:
        q = broadcaster.subscribe()
        try:
            # Initial ping so the browser knows the connection is alive
            yield ": connected\n\n"
            while True:
                event = await q.get()
                yield f"event: {event}\ndata: \n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Mentor board
# ---------------------------------------------------------------------------

def _format_mentor_sessions(sessions) -> list[dict]:
    """Return list of currently signed-in mentors with elapsed time."""
    result = []
    for s in sessions:
        elapsed = datetime.utcnow() - s.sign_in_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes = remainder // 60
        result.append({
            "name": s.mentor.name,
            "team": s.mentor.team.number if s.mentor.team else None,
            "elapsed": f"{hours}h {minutes:02d}m" if hours else f"{minutes}m",
        })
    return result


@router.get("/mentor", response_class=HTMLResponse)
async def mentor_board(request: Request, db: AsyncSession = Depends(get_db)):
    sessions = await get_signed_in_mentors(db)
    signed_in = _format_mentor_sessions(sessions)
    return templates.TemplateResponse("mentor.html", {
        "request": request,
        "signed_in": signed_in,
    })


@router.get("/mentor/data")
async def mentor_data(db: AsyncSession = Depends(get_db)):
    sessions = await get_signed_in_mentors(db)
    return {"signed_in": _format_mentor_sessions(sessions)}


@router.get("/mentor/stats")
async def mentor_stats(db: AsyncSession = Depends(get_db)):
    """Return mentor leaderboard: all-time, this week, longest session, longest streak."""
    week_start = today_local() - timedelta(days=today_local().weekday())
    week_start_utc = local_to_utc(datetime.combine(week_start, datetime.min.time()))

    # ── 1. All-time top hours ─────────────────────────────────────────────────
    alltime_rows = await db.execute(
        select(Mentor.name, func.sum(MentorSession.hours_counted).label("total"))
        .join(MentorSession, MentorSession.mentor_id == Mentor.id)
        .where(MentorSession.hours_counted.isnot(None))
        .group_by(Mentor.id)
        .order_by(func.sum(MentorSession.hours_counted).desc())
        .limit(3)
    )
    alltime = [{"name": r.name, "value": f"{r.total:.1f}h"} for r in alltime_rows]

    # ── 2. This week top hours ────────────────────────────────────────────────
    week_rows = await db.execute(
        select(Mentor.name, func.sum(MentorSession.hours_counted).label("total"))
        .join(MentorSession, MentorSession.mentor_id == Mentor.id)
        .where(
            MentorSession.hours_counted.isnot(None),
            MentorSession.sign_in_time >= week_start_utc,
        )
        .group_by(Mentor.id)
        .order_by(func.sum(MentorSession.hours_counted).desc())
        .limit(3)
    )
    week = [{"name": r.name, "value": f"{r.total:.1f}h"} for r in week_rows]

    # ── 3. Longest single session ─────────────────────────────────────────────
    longest_rows = await db.execute(
        select(Mentor.name, func.max(MentorSession.hours_counted).label("max_h"))
        .join(MentorSession, MentorSession.mentor_id == Mentor.id)
        .where(MentorSession.hours_counted.isnot(None))
        .group_by(Mentor.id)
        .order_by(func.max(MentorSession.hours_counted).desc())
        .limit(3)
    )
    longest = [{"name": r.name, "value": f"{r.max_h:.1f}h"} for r in longest_rows]

    # ── 4. Longest streak (consecutive days with >= 1 h) ──────────────────────
    streak_rows = (
        await db.execute(
            select(
                Mentor.id,
                Mentor.name,
                MentorSession.sign_in_time,
                MentorSession.hours_counted,
            )
            .join(MentorSession, MentorSession.mentor_id == Mentor.id)
            .where(MentorSession.hours_counted.isnot(None))
            .where(MentorSession.sign_out_time.isnot(None))
        )
    ).all()

    mentor_daily: dict[int, dict] = defaultdict(lambda: defaultdict(float))
    mentor_names: dict[int, str] = {}
    for row in streak_rows:
        mentor_names[row.id] = row.name
        mentor_daily[row.id][utc_to_local(row.sign_in_time).date()] += row.hours_counted or 0.0

    streaks: list[tuple[int, str]] = []
    for mid, daily in mentor_daily.items():
        qualifying = sorted(d for d, h in daily.items() if h >= 1.0)
        if not qualifying:
            continue
        best = current = 1
        for i in range(1, len(qualifying)):
            if (qualifying[i] - qualifying[i - 1]).days == 1:
                current += 1
                best = max(best, current)
            else:
                current = 1
        streaks.append((best, mentor_names[mid]))

    streaks.sort(reverse=True)
    streak = [{"name": name, "value": f"{s}d"} for s, name in streaks[:3]]

    return {
        "alltime": alltime,
        "week": week,
        "longest_session": longest,
        "streak": streak,
    }


@router.get("/mentor/stream")
async def mentor_stream():
    """SSE endpoint — pushes 'mentor_update' events to connected mentor boards."""

    async def event_generator() -> AsyncGenerator[str, None]:
        q = broadcaster.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                event = await q.get()
                if event == "mentor_update":
                    yield f"event: mentor_update\ndata: \n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
