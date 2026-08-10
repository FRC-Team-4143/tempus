"""
Kiosk routes — display boards, badge POST, SSE streams, leaderboard stats, and
the device-pairing flow that gates all of it (see `services/kiosk_device.py`).

`/kiosk` is the combined auto-swapping display the shop kiosk points at; the two
pinned boards live at `/kiosk/student` and `/kiosk/mentor`. Everything under
`/kiosk/*` is a paired-display surface except `/kiosk/demo` (fake names only) and
`/kiosk/pair/status` (the pairing poll).
"""
import json
from collections import defaultdict
from datetime import date
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    AttendanceSession, KioskDeviceStatus, MentorSession, Mentor, Student, Team,
)
from app.schemas import SignInRequest, SignInResponse
from app.services import kiosk_device
from app.services.attendance import (
    sign_in, get_signed_in_students,
    mentor_sign_in, get_signed_in_mentors,
)
from app.services.broadcaster import broadcaster
from app.utils import utc_to_local, today_local, format_elapsed, current_week_bounds

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)


async def _is_paired(db: AsyncSession, request: Request) -> bool:
    """True if this browser holds a cookie naming an *active* kiosk device."""
    device = await kiosk_device.current_device(db, request)
    if device is None or device.status != KioskDeviceStatus.active:
        return False
    await kiosk_device.touch(db, device, request)
    return True


async def _require_paired_device(
    request: Request, db: AsyncSession = Depends(get_db)
) -> None:
    """Route dependency for the JSON/SSE board endpoints — 403 when unpaired.

    The HTML board pages don't use this: they render the pairing page instead, so
    an operator standing at the display sees what to do rather than a JSON error.
    """
    if not await _is_paired(db, request):
        raise HTTPException(status_code=403, detail="This display is not paired.")


async def _pairing_gate(
    request: Request, db: AsyncSession, board_label: str
) -> Optional[Response]:
    """None if paired; otherwise the pairing page to return instead of the board.

    Order matters. Reusing a live pending row first means a display that reloads (or
    whose poll races an approval) never creates a second row — so the only rows that
    exist are genuine first-time requests. Everything after that is abuse control:
    pairing is closed by default, and even while open it is throttled per-IP and
    capped, because these pages are necessarily reachable by anyone.
    """
    if await _is_paired(db, request):
        return None

    def _page(**ctx) -> Response:
        return templates.TemplateResponse(
            "kiosk_pair.html",
            {"request": request, "board_label": board_label, **ctx},
        )

    device = await kiosk_device.current_device(db, request)
    if device is not None and device.status == KioskDeviceStatus.pending:
        return _page(user_code=device.user_code)

    if await kiosk_device.pairing_open_until(db) is None:
        return _page(user_code=None, reason="closed")

    # Commit the prune on its own so it sticks even when the checks below bail
    # out — otherwise a throttled request would roll back the cleanup it just did.
    await kiosk_device.prune_pending(db)
    await db.commit()

    if not kiosk_device.rate_limit_ok(kiosk_device.client_ip(request)):
        return _page(user_code=None, reason="throttled")
    if await kiosk_device.pending_count(db) >= kiosk_device.MAX_PENDING:
        return _page(user_code=None, reason="throttled")

    device = await kiosk_device.create_pending(db, request)
    await db.commit()
    response = _page(user_code=device.user_code)
    kiosk_device.issue_cookie(response, device.device_id)
    return response


def _format_sessions(sessions) -> dict:
    """Return signed-in students grouped by team number."""
    by_team: dict[int, list[dict]] = {}
    for s in sessions:
        team_number = s.student.team.number
        if team_number not in by_team:
            by_team[team_number] = []
        by_team[team_number].append(
            {
                "session_id": s.id,
                "name": s.student.name,
                "sign_in_time": utc_to_local(s.sign_in_time).strftime("%I:%M %p"),
                "elapsed": format_elapsed(s.sign_in_time),
            }
        )
    return by_team


@router.get("/kiosk", response_class=HTMLResponse)
async def kiosk_page(request: Request, db: AsyncSession = Depends(get_db)):
    """The combined auto-swapping display — what the shop kiosk points at.

    The hardware has no mouse or keyboard, only a QR scanner, so the mentor board
    can't be reached by clicking. Instead a mentor badge scan swaps this display to
    the mentor board for `kiosk_mentor_hold_seconds`, and any student scan snaps it
    straight back. Both boards render here; the swap is client-side.
    """
    if page := await _pairing_gate(request, db, "Kiosk"):
        return page
    sessions = await get_signed_in_students(db)
    mentor_sessions = await get_signed_in_mentors(db)
    return templates.TemplateResponse(
        "kiosk_combined.html",
        {
            "request": request,
            "by_team": _format_sessions(sessions),
            "teams": [4143, 4423],
            "signed_in": _format_mentor_sessions(mentor_sessions),
            "mentor_hold_seconds": settings.kiosk_mentor_hold_seconds,
        },
    )


@router.get("/kiosk/student", response_class=HTMLResponse)
async def kiosk_student_board(request: Request, db: AsyncSession = Depends(get_db)):
    """The student board, pinned — for a dedicated second screen, or laptop debugging."""
    if page := await _pairing_gate(request, db, "Student Board"):
        return page
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


@router.get("/kiosk/pair/status")
async def kiosk_pair_status(request: Request, db: AsyncSession = Depends(get_db)):
    """Poll target for the pairing page. Deliberately read-only — it never creates a
    row, so a display polling every 3s can't be an amplification vector."""
    device = await kiosk_device.current_device(db, request)
    return {"status": device.status.value if device else "unknown"}


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
    # Soft 200 rather than a 403: the kiosk shows `message` in its toast, so an
    # unpaired display tells the student what's wrong instead of failing silently.
    if not await _is_paired(db, request):
        return SignInResponse(success=False, message="This display is not paired.")

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


@router.get("/kiosk/student/data", dependencies=[Depends(_require_paired_device)])
async def kiosk_data(db: AsyncSession = Depends(get_db)):
    """JSON snapshot of currently signed-in students, grouped by team number."""
    sessions = await get_signed_in_students(db)
    by_team = _format_sessions(sessions)
    # Ensure both teams are always present in the response
    for t in [4143, 4423]:
        by_team.setdefault(t, [])
    return by_team


@router.get("/kiosk/student/stats", dependencies=[Depends(_require_paired_device)])
async def kiosk_stats(db: AsyncSession = Depends(get_db)):
    """Return leaderboard stats for the kiosk stats panel."""

    # Leaderboard cutoff — count cumulative stats only from this point (None = all-time)
    from app.services.app_settings import get_leaderboard_since, leaderboard_since_utc
    leaderboard_since = await get_leaderboard_since(db)
    since_utc = await leaderboard_since_utc(db)

    # ── 1. All-time top hours (since cutoff) ──────────────────────────────────
    alltime_q = (
        select(Student.name, func.sum(AttendanceSession.hours_counted).label("total"))
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(Student.is_active.is_(True))
    )
    if since_utc is not None:
        alltime_q = alltime_q.where(AttendanceSession.sign_in_time >= since_utc)
    alltime_result = await db.execute(
        alltime_q
        .group_by(Student.id)
        .order_by(func.sum(AttendanceSession.hours_counted).desc())
        .limit(3)
    )
    alltime = [{"name": r.name, "value": f"{r.total:.1f}h"} for r in alltime_result]

    # ── 2. This week top hours (Mon–Sun, CST) ─────────────────────────────────
    week_start_utc, _ = current_week_bounds()
    week_result = await db.execute(
        select(Student.name, func.sum(AttendanceSession.hours_counted).label("total"))
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(Student.is_active.is_(True))
        .where(
            AttendanceSession.sign_in_time >= week_start_utc
        )
        .group_by(Student.id)
        .order_by(func.sum(AttendanceSession.hours_counted).desc())
        .limit(3)
    )
    week = [{"name": r.name, "value": f"{r.total:.1f}h"} for r in week_result]

    # ── 3. Longest single session (since cutoff) ──────────────────────────────
    longest_q = (
        select(Student.name, func.max(AttendanceSession.hours_counted).label("max_h"))
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(Student.is_active.is_(True))
    )
    if since_utc is not None:
        longest_q = longest_q.where(AttendanceSession.sign_in_time >= since_utc)
    longest_result = await db.execute(
        longest_q
        .group_by(Student.id)
        .order_by(func.max(AttendanceSession.hours_counted).desc())
        .limit(3)
    )
    longest = [{"name": r.name, "value": f"{r.max_h:.1f}h"} for r in longest_result]

    # ── 4. Longest streak (consecutive days with >= 1 h, since cutoff) ────────
    streak_q = (
        select(
            Student.id,
            Student.name,
            AttendanceSession.sign_in_time,
            AttendanceSession.hours_counted,
        )
        .join(Student, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(AttendanceSession.sign_out_time.isnot(None))
        .where(Student.is_active.is_(True))
    )
    if since_utc is not None:
        streak_q = streak_q.where(AttendanceSession.sign_in_time >= since_utc)
    streak_rows = (await db.execute(streak_q)).all()

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

    # ── 5. Team totals (students only, since cutoff) ─────────────────────────
    team_totals_q = (
        select(Team.number, func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0).label("total"))
        .join(Student, Student.team_id == Team.id)
        .join(AttendanceSession, AttendanceSession.student_id == Student.id)
        .where(AttendanceSession.hours_counted.isnot(None))
        .where(Student.is_active.is_(True))
        .where(Team.number.in_([4143, 4423]))
    )
    if since_utc is not None:
        team_totals_q = team_totals_q.where(AttendanceSession.sign_in_time >= since_utc)
    team_totals_result = await db.execute(
        team_totals_q
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
        "leaderboard_since": leaderboard_since.isoformat() if leaderboard_since else None,
    }


@router.get("/kiosk/student/stream", dependencies=[Depends(_require_paired_device)])
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
        result.append({
            "name": s.mentor.name,
            "team": s.mentor.team.number if s.mentor.team else None,
            "elapsed": format_elapsed(s.sign_in_time),
        })
    return result


@router.get("/kiosk/mentor", response_class=HTMLResponse)
async def mentor_board(request: Request, db: AsyncSession = Depends(get_db)):
    """The mentor board, pinned. `/kiosk` swaps to this view on its own; this route
    is for a screen dedicated to it."""
    if page := await _pairing_gate(request, db, "Mentor Board"):
        return page
    sessions = await get_signed_in_mentors(db)
    signed_in = _format_mentor_sessions(sessions)
    return templates.TemplateResponse("mentor.html", {
        "request": request,
        "signed_in": signed_in,
    })


@router.get("/mentor", include_in_schema=False)
async def mentor_board_legacy():
    """Pre-restructure URL. Redirects unconditionally — the pairing check happens at
    the destination, so this alias can't drift from the real gate."""
    return RedirectResponse("/kiosk/mentor", status_code=302)


@router.get("/kiosk/mentor/data", dependencies=[Depends(_require_paired_device)])
async def mentor_data(db: AsyncSession = Depends(get_db)):
    sessions = await get_signed_in_mentors(db)
    return {"signed_in": _format_mentor_sessions(sessions)}


@router.get("/kiosk/mentor/stats", dependencies=[Depends(_require_paired_device)])
async def mentor_stats(db: AsyncSession = Depends(get_db)):
    """Return mentor leaderboard: all-time, this week, longest session, longest streak."""
    week_start_utc, _ = current_week_bounds()

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


@router.get("/kiosk/mentor/stream", dependencies=[Depends(_require_paired_device)])
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
