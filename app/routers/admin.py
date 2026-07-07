"""
Admin routes — password-protected web UI.

Auth: session cookie signed with itsdangerous.
"""
import csv
import io
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote

log = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    AttendanceSession, AuditLog, Mentor, MentorSession, SessionStatus, Student, Subteam, Team, WeeklyRequirement,
)
from app.services import audit
from app.services.sso import logout_url, make_authorize_url, sso_identity
from app.utils import utc_to_local, today_local, local_to_utc

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)

# ── Auth helpers ───────────────────────────────────────────────────────────────
#
# /admin is gated by Legion SSO: the shared `mw_sso` cookie must carry the `tempus-admin`
# group. There is no local password — Legion mints the cookie, Tempus only verifies it
# (services/sso.py). The first admin is granted `tempus-admin` in Legion's /admin/groups.

_ADMIN_GROUP = "tempus-admin"


def _require_groups(request: Request, groups: set[str]):
    """Gate a route on the SSO identity holding at least one of `groups`. Returns a
    redirect (no/invalid cookie → sign in at Legion) or a 403 (signed in but not
    authorized) to short-circuit the route with, or None to let it proceed."""
    identity = sso_identity(request)
    if identity is None:
        return RedirectResponse(make_authorize_url(request), status_code=303)
    if groups & set(identity.get("groups") or []):
        return None
    return templates.TemplateResponse(
        "admin/forbidden.html",
        {"request": request, "name": identity.get("name", "")},
        status_code=403,
    )


def _require_auth(request: Request):
    """Full admin access: the `tempus-admin` group via Legion SSO."""
    return _require_groups(request, {_ADMIN_GROUP})


async def _active_subteams(db: AsyncSession):
    """Active subteams (Legion mirror) for dropdowns, ordered for display."""
    return (await db.execute(
        select(Subteam).where(Subteam.is_active.is_(True))
        .order_by(Subteam.sort_order, Subteam.label)
    )).scalars().all()


# ── Logout ─────────────────────────────────────────────────────────────────────

@router.get("/logout")
async def admin_logout(request: Request):
    # Single logout: bounce to Legion's /sso/logout, which clears the shared `mw_sso`
    # cookie for every sibling app.
    return RedirectResponse(logout_url(request), status_code=303)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    # Currently signed in (exclude pending-checkout sessions)
    signed_in_result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student).selectinload(Student.team))
        .where(
            AttendanceSession.sign_out_time.is_(None),
            AttendanceSession.checkout_requested_at.is_(None),
        )
        .order_by(AttendanceSession.sign_in_time)
    )
    signed_in = signed_in_result.scalars().all()

    # Leaderboard: total hours per student, counted from the configured cutoff.
    # The date condition lives in the outer-join ON clause so students with zero
    # qualifying sessions still appear.
    from app.services.app_settings import get_leaderboard_since, leaderboard_since_utc
    leaderboard_since = await get_leaderboard_since(db)
    since_utc = await leaderboard_since_utc(db)

    join_clause = AttendanceSession.student_id == Student.id
    if since_utc is not None:
        join_clause = and_(join_clause, AttendanceSession.sign_in_time >= since_utc)

    lboard_result = await db.execute(
        select(
            Student.id,
            Student.name,
            Team.number.label("team_number"),
            func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0).label("total"),
        )
        .join(AttendanceSession, join_clause, isouter=True)
        .join(Team, Team.id == Student.team_id)
        .where(Student.is_active.is_(True))
        .group_by(Student.id)
        .order_by(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0).desc())
    )
    leaderboard = lboard_result.all()

    # Active students not currently signed in (for manual sign-in dropdown)
    signed_in_ids = {s.student_id for s in signed_in}
    all_active_result = await db.execute(
        select(Student)
        .options(selectinload(Student.team))
        .where(Student.is_active.is_(True))
        .order_by(Student.name)
    )
    all_active = all_active_result.scalars().all()
    not_signed_in = [s for s in all_active if s.id not in signed_in_ids]

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "signed_in": signed_in,
            "leaderboard": leaderboard,
            "leaderboard_since": leaderboard_since,
            "not_signed_in": not_signed_in,
        },
    )


# ── Manual sign-in ────────────────────────────────────────────────────────────

@router.post("/manual-signin")
async def admin_manual_signin(
    request: Request,
    student_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.attendance import get_open_session
    from app.services.broadcaster import broadcaster

    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalars().first()
    if student:
        open_session = await get_open_session(db, student.id)
        if not open_session:
            db.add(AttendanceSession(
                student_id=student.id,
                sign_in_time=datetime.utcnow(),
            ))
            await db.commit()
            await broadcaster.broadcast("update")

    return RedirectResponse("/admin", status_code=303)


# ── Students ───────────────────────────────────────────────────────────────────

# ── Roster (read-only mirror of Legion) ─────────────────────────────────────────
#
# The roster is owned by Legion and synced in (see services/legion_sync.py). Tempus no
# longer creates/edits/archives members — manage them in Legion's /admin. These routes
# are read-only plus a manual "Sync now" and the QR-badge send.

@router.get("/roster", response_class=HTMLResponse)
@router.get("/students", response_class=HTMLResponse)  # legacy path → roster
async def admin_roster(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    students = (await db.execute(
        select(Student).options(selectinload(Student.team)).order_by(Student.name)
    )).scalars().all()
    mentors = (await db.execute(
        select(Mentor).options(selectinload(Mentor.team)).order_by(Mentor.name)
    )).scalars().all()

    from app.services.app_settings import get_setting
    last_synced = await get_setting(db, "legion_last_synced_at")

    return templates.TemplateResponse(
        "admin/roster.html",
        {
            "request": request,
            "students": students,
            "mentors": mentors,
            "last_synced": last_synced,
            "legion_base_url": settings.legion_base_url,
        },
    )


@router.post("/roster/sync")
async def admin_roster_sync(request: Request, db: AsyncSession = Depends(get_db)):
    """Manually trigger a roster pull from Legion."""
    if redirect := _require_auth(request):
        return redirect

    from app.services.legion_sync import sync_roster
    try:
        summary = await sync_roster(db)
    except Exception as e:  # network / config errors surface as a flash, not a 500
        log.error("Manual Legion sync failed: %s", e)
        await audit.record(db, request, "roster.sync_failed", f"Legion sync failed: {e}")
        await db.commit()
        return RedirectResponse(f"/admin/roster?sync_error={quote(str(e))}", status_code=303)
    await audit.record(db, request, "roster.sync", f"Synced roster from Legion ({summary})")
    await db.commit()
    return RedirectResponse(f"/admin/roster?synced={quote(summary)}", status_code=303)


@router.post("/students/send-qr-all")
async def admin_students_send_qr_all(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.slack_client import send_qr_dm
    result = await db.execute(
        select(Student).where(
            Student.slack_user_id.is_not(None),
            Student.is_active.is_(True),
        )
    )
    students = [s for s in result.scalars().all() if (s.member_code or s.student_code)]
    for s in students:
        background_tasks.add_task(send_qr_dm, s.slack_user_id, s.member_code or s.student_code, s.name)
    return RedirectResponse(f"/admin/roster?qr_sent={len(students)}", status_code=303)


@router.post("/mentors/send-qr-all")
async def admin_mentors_send_qr_all(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.slack_client import send_qr_dm
    result = await db.execute(
        select(Mentor).where(
            Mentor.slack_user_id.is_not(None),
            Mentor.is_active.is_(True),
        )
    )
    mentors = [m for m in result.scalars().all() if (m.member_code or m.mentor_code)]
    for m in mentors:
        background_tasks.add_task(send_qr_dm, m.slack_user_id, m.member_code or m.mentor_code, m.name)
    return RedirectResponse(f"/admin/roster?qr_sent={len(mentors)}", status_code=303)


# ── Weekly Requirements ────────────────────────────────────────────────────────

@router.get("/requirements", response_class=HTMLResponse)
async def admin_requirements_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(WeeklyRequirement)
        .options(selectinload(WeeklyRequirement.team))
        .order_by(WeeklyRequirement.week_start.desc())
    )
    requirements = result.scalars().all()

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    subteams = await _active_subteams(db)

    # Compute per-row "covers until" by grouping entries in each (team_id, subteam) scope
    from collections import defaultdict
    scope_entries: dict = defaultdict(list)
    for r in sorted(requirements, key=lambda x: x.week_start):
        scope_entries[(r.team_id, r.subteam_slug)].append(r)

    covers_until: dict = {}
    for entries in scope_entries.values():
        for i, r in enumerate(entries):
            if i + 1 < len(entries):
                covers_until[r.id] = entries[i + 1].week_start - timedelta(days=1)
            else:
                covers_until[r.id] = None  # ongoing

    return templates.TemplateResponse(
        "admin/requirements.html",
        {
            "request": request,
            "requirements": requirements,
            "teams": teams,
            "subteams": subteams,
            "covers_until": covers_until,
        },
    )


@router.post("/requirements")
async def admin_requirements_create(
    request: Request,
    team_id: Optional[str] = Form(None),
    subteam_slug: Optional[str] = Form(None),
    week_start: date = Form(...),
    required_hours: float = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    # Normalise to Monday
    week_monday = week_start - timedelta(days=week_start.weekday())
    parsed_slug = subteam_slug.strip() if subteam_slug and subteam_slug.strip() else None
    parsed_team_id = int(team_id) if team_id else None  # empty = all teams

    # Upsert: update if exists
    team_clause = (
        WeeklyRequirement.team_id.is_(None) if parsed_team_id is None
        else WeeklyRequirement.team_id == parsed_team_id
    )
    slug_clause = (
        WeeklyRequirement.subteam_slug.is_(None) if parsed_slug is None
        else WeeklyRequirement.subteam_slug == parsed_slug
    )
    existing_result = await db.execute(
        select(WeeklyRequirement).where(
            team_clause,
            slug_clause,
            WeeklyRequirement.week_start == week_monday,
        )
    )
    existing = existing_result.scalars().first()
    if existing:
        existing.required_hours = required_hours
    else:
        db.add(
            WeeklyRequirement(
                team_id=parsed_team_id, subteam_slug=parsed_slug, week_start=week_monday, required_hours=required_hours
            )
        )
    await audit.record(
        db, request, "requirement.set",
        f"Set requirement {required_hours}h for team={parsed_team_id or 'all'} "
        f"subteam={parsed_slug or 'all'} week {week_monday}",
        entity_type="requirement",
        detail={"team_id": parsed_team_id,
                "subteam_slug": parsed_slug,
                "week_start": str(week_monday), "required_hours": required_hours},
    )
    await db.commit()
    return RedirectResponse("/admin/requirements", status_code=303)


@router.post("/requirements/{req_id}/delete")
async def admin_requirements_delete(
    req_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(WeeklyRequirement)
        .options(selectinload(WeeklyRequirement.team))
        .where(WeeklyRequirement.id == req_id)
    )
    req = result.scalar_one_or_none()
    if not req:
        return RedirectResponse("/admin/requirements", status_code=303)

    team_label = f"team {req.team.number}" if req.team else "all teams"
    subteam_label = req.subteam_slug or "all subteams"
    await audit.record(
        db, request, "requirement.delete",
        f"Deleted requirement: {req.required_hours}h for {team_label}, {subteam_label}, week {req.week_start}",
        entity_type="requirement", entity_id=req_id,
    )
    await db.execute(delete(WeeklyRequirement).where(WeeklyRequirement.id == req_id))
    await db.commit()
    return RedirectResponse("/admin/requirements", status_code=303)


# ── Sessions ───────────────────────────────────────────────────────────────────

@router.get("/sessions", response_class=HTMLResponse)
async def admin_sessions_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    person_type: Optional[str] = "student",
    student_id: Optional[str] = None,
    team_id: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if redirect := _require_auth(request):
        return redirect

    is_mentor = (person_type or "student").strip().lower() == "mentor"

    # Sanitize integer params — empty string from blank form select becomes None
    sid = int(student_id) if student_id and student_id.strip().isdigit() else None
    mid = sid if is_mentor else None
    tid = int(team_id) if team_id and team_id.strip().isdigit() else None
    cat_str = category.strip() if category and category.strip() else None
    cat = cat_str  # subteam slug (free-form, sourced from Legion)
    try:
        d_from = date.fromisoformat(date_from.strip()) if date_from and date_from.strip() else None
    except ValueError:
        d_from = None
    try:
        d_to = date.fromisoformat(date_to.strip()) if date_to and date_to.strip() else None
    except ValueError:
        d_to = None

    PAGE_SIZE = 50

    if is_mentor:
        query = (
            select(MentorSession)
            .options(selectinload(MentorSession.mentor).selectinload(Mentor.team))
            .order_by(MentorSession.sign_in_time.desc())
        )
        if mid:
            query = query.where(MentorSession.mentor_id == mid)
        if tid or cat:
            query = query.join(Mentor)
            if tid:
                query = query.where(Mentor.team_id == tid)
            if cat:
                query = query.where(Mentor.subteam_slug == cat)
        if d_from:
            query = query.where(
                MentorSession.sign_in_time >= local_to_utc(datetime.combine(d_from, datetime.min.time()))
            )
        if d_to:
            query = query.where(
                MentorSession.sign_in_time <= local_to_utc(datetime.combine(d_to, datetime.max.time()))
            )
        total_result = await db.execute(select(func.count()).select_from(query.subquery()))
        total = total_result.scalar() or 0
        sessions_result = await db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
        sessions = sessions_result.scalars().all()
        all_students = (await db.execute(select(Mentor).order_by(Mentor.name))).scalars().all()
    else:
        query = (
            select(AttendanceSession)
            .options(selectinload(AttendanceSession.student).selectinload(Student.team))
            .order_by(AttendanceSession.sign_in_time.desc())
        )
        if sid:
            query = query.where(AttendanceSession.student_id == sid)
        if tid or cat:
            query = query.join(Student)
            if tid:
                query = query.where(Student.team_id == tid)
            if cat:
                query = query.where(Student.subteam_slug == cat)
        if d_from:
            query = query.where(
                AttendanceSession.sign_in_time >= local_to_utc(datetime.combine(d_from, datetime.min.time()))
            )
        if d_to:
            query = query.where(
                AttendanceSession.sign_in_time <= local_to_utc(datetime.combine(d_to, datetime.max.time()))
            )
        total_result = await db.execute(select(func.count()).select_from(query.subquery()))
        total = total_result.scalar() or 0
        sessions_result = await db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
        sessions = sessions_result.scalars().all()
        all_students = (await db.execute(select(Student).order_by(Student.name))).scalars().all()

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    return templates.TemplateResponse(
        "admin/sessions.html",
        {
            "request": request,
            "sessions": sessions,
            "is_mentor": is_mentor,
            "page": page,
            "total": total,
            "page_size": PAGE_SIZE,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "all_students": all_students,
            "teams": teams,
            "subteams": await _active_subteams(db),
            "filters": {
                "person_type": "mentor" if is_mentor else "student",
                "student_id": mid if is_mentor else sid,
                "team_id": tid,
                "category": cat,
                "date_from": d_from,
                "date_to": d_to,
            },
        },
    )


@router.get("/sessions/export")
async def admin_sessions_export(
    request: Request,
    db: AsyncSession = Depends(get_db),
    student_id: Optional[str] = None,
    team_id: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if redirect := _require_auth(request):
        return redirect

    sid = int(student_id) if student_id and student_id.strip().isdigit() else None
    tid = int(team_id) if team_id and team_id.strip().isdigit() else None
    cat_str = category.strip() if category and category.strip() else None
    cat = cat_str  # subteam slug (free-form, sourced from Legion)
    try:
        d_from = date.fromisoformat(date_from.strip()) if date_from and date_from.strip() else None
    except ValueError:
        d_from = None
    try:
        d_to = date.fromisoformat(date_to.strip()) if date_to and date_to.strip() else None
    except ValueError:
        d_to = None

    query = (
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student).selectinload(Student.team))
        .order_by(AttendanceSession.sign_in_time.desc())
    )
    if sid:
        query = query.where(AttendanceSession.student_id == sid)
    if tid or cat:
        query = query.join(Student)
        if tid:
            query = query.where(Student.team_id == tid)
        if cat:
            query = query.where(Student.subteam_slug == cat)
    if d_from:
        query = query.where(
            AttendanceSession.sign_in_time >= local_to_utc(datetime.combine(d_from, datetime.min.time()))
        )
    if d_to:
        query = query.where(
            AttendanceSession.sign_in_time <= local_to_utc(datetime.combine(d_to, datetime.max.time()))
        )

    result = await db.execute(query)
    sessions = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Session ID", "Student", "Team", "Sign In", "Sign Out", "Status", "Hours Counted"]
    )
    for s in sessions:
        writer.writerow(
            [
                s.id,
                s.student.name,
                s.student.team.number,
                utc_to_local(s.sign_in_time).isoformat() if s.sign_in_time else "",
                utc_to_local(s.sign_out_time).isoformat() if s.sign_out_time else "",
                s.status.value if s.status else "",
                s.hours_counted if s.hours_counted is not None else "",
            ]
        )

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sessions.csv"},
    )


@router.get("/sessions/{session_id}/edit")
async def admin_sessions_edit_form(
    session_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student).selectinload(Student.team))
        .where(AttendanceSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        return RedirectResponse("/admin/sessions", status_code=303)

    return templates.TemplateResponse(
        "admin/session_edit.html",
        {"request": request, "s": session, "is_mentor": False,
         "statuses": [s for s in SessionStatus if s != SessionStatus.auto]},
    )


@router.post("/sessions/{session_id}/edit")
async def admin_sessions_edit(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sign_in_time: str = Form(...),
    sign_out_time: str = Form(""),
    status: str = Form(""),
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student))
        .where(AttendanceSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        return RedirectResponse("/admin/sessions", status_code=303)

    before = {
        "sign_in_time": str(session.sign_in_time),
        "sign_out_time": str(session.sign_out_time),
        "status": session.status.value if session.status else None,
        "hours_counted": session.hours_counted,
    }

    parsed_sign_in = local_to_utc(datetime.fromisoformat(sign_in_time))
    parsed_sign_out = local_to_utc(datetime.fromisoformat(sign_out_time)) if sign_out_time else None
    parsed_status = SessionStatus(status) if status else None

    session.sign_in_time = parsed_sign_in
    session.sign_out_time = parsed_sign_out
    session.status = parsed_status

    if parsed_sign_out and parsed_status:
        from app.services.attendance import _status_multiplier
        elapsed_hours = (parsed_sign_out - parsed_sign_in).total_seconds() / 3600.0
        session.hours_counted = round(elapsed_hours * _status_multiplier(parsed_status), 4)
    else:
        session.hours_counted = None

    after = {
        "sign_in_time": str(session.sign_in_time),
        "sign_out_time": str(session.sign_out_time),
        "status": session.status.value if session.status else None,
        "hours_counted": session.hours_counted,
    }
    date_str = utc_to_local(session.sign_in_time).strftime("%b %d")
    status_label = session.status.value.capitalize() if session.status else "None"
    await audit.record(
        db, request, "session.edit",
        f"admin changed {session.student.name}'s session ({date_str}) to {status_label} via Admin",
        entity_type="session", entity_id=session_id,
        detail={"before": before, "after": after},
    )
    await db.commit()
    return RedirectResponse("/admin/sessions", status_code=303)


@router.post("/sessions/{session_id}/delete")
async def admin_sessions_delete(
    session_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student))
        .where(AttendanceSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        return RedirectResponse("/admin/sessions", status_code=303)

    date_str = utc_to_local(session.sign_in_time).strftime("%b %d %I:%M %p")
    hours = f"{session.hours_counted:.2f}h" if session.hours_counted is not None else "open, no hours"
    await audit.record(
        db, request, "session.delete",
        f"Deleted {session.student.name}'s session ({date_str}, {hours})",
        entity_type="session", entity_id=session_id,
    )
    await db.execute(delete(AttendanceSession).where(AttendanceSession.id == session_id))
    await db.commit()
    return RedirectResponse("/admin/sessions", status_code=303)


@router.post("/sessions/{session_id}/force-signout")
async def admin_sessions_force_signout(
    session_id: int,
    request: Request,
    send_meme: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student))
        .where(AttendanceSession.id == session_id)
    )
    att = result.scalar_one_or_none()
    if not att or att.sign_out_time is not None:
        return RedirectResponse("/admin/sessions", status_code=303)

    from app.services.attendance import sign_out
    await sign_out(db, session_id, SessionStatus.auto)

    from app.services.broadcaster import broadcaster
    await broadcaster.broadcast("update")

    if send_meme and settings.slack_announce_channel and att.student:
        first_name = att.student.name.split()[0]
        slack_id = att.student.slack_user_id
        mention = f"<@{slack_id}>" if slack_id else first_name
        try:
            from app.services.roast import fetch_meme
            from app.services.slack_client import send_channel_image
            img = await fetch_meme([first_name])
            await send_channel_image(
                settings.slack_announce_channel,
                img,
                f"{first_name.lower()}_wall_of_shame.png",
                comment=f"🚨 {mention} forgot to sign out 😅",
            )
        except Exception as e:
            log.error("Force sign-out meme failed for %s: %s", first_name, e)

    return RedirectResponse("/admin/sessions", status_code=303)


@router.post("/mentor-sessions/{session_id}/force-signout")
async def admin_mentor_sessions_force_signout(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.attendance import mentor_sign_out
    session = await mentor_sign_out(db, session_id)
    if session is None:
        return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)

    from app.services.broadcaster import broadcaster
    await broadcaster.broadcast("mentor_update")

    return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)


@router.get("/mentor-sessions/{session_id}/edit")
async def admin_mentor_sessions_edit_form(
    session_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(MentorSession)
        .options(selectinload(MentorSession.mentor).selectinload(Mentor.team))
        .where(MentorSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)

    return templates.TemplateResponse(
        "admin/session_edit.html",
        {"request": request, "s": session, "is_mentor": True},
    )


@router.post("/mentor-sessions/{session_id}/edit")
async def admin_mentor_sessions_edit(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sign_in_time: str = Form(...),
    sign_out_time: str = Form(""),
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(MentorSession)
        .options(selectinload(MentorSession.mentor))
        .where(MentorSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)

    before = {
        "sign_in_time": str(session.sign_in_time),
        "sign_out_time": str(session.sign_out_time),
        "hours_counted": session.hours_counted,
    }

    parsed_sign_in = local_to_utc(datetime.fromisoformat(sign_in_time))
    parsed_sign_out = local_to_utc(datetime.fromisoformat(sign_out_time)) if sign_out_time else None

    session.sign_in_time = parsed_sign_in
    session.sign_out_time = parsed_sign_out
    if parsed_sign_out:
        session.hours_counted = round((parsed_sign_out - parsed_sign_in).total_seconds() / 3600.0, 4)
    else:
        session.hours_counted = None

    after = {
        "sign_in_time": str(session.sign_in_time),
        "sign_out_time": str(session.sign_out_time),
        "hours_counted": session.hours_counted,
    }
    in_str = utc_to_local(session.sign_in_time).strftime("%b %d %I:%M %p")
    out_str = utc_to_local(session.sign_out_time).strftime("%I:%M %p") if session.sign_out_time else "open"
    hours = f"{session.hours_counted:.2f}h" if session.hours_counted is not None else "no hours"
    await audit.record(
        db, request, "mentor_session.edit",
        f"admin edited {session.mentor.name}'s mentor session to {in_str} → {out_str} ({hours}) via Admin",
        entity_type="mentor_session", entity_id=session_id,
        detail={"before": before, "after": after},
    )
    await db.commit()
    return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)


@router.post("/mentor-sessions/{session_id}/delete")
async def admin_mentor_sessions_delete(
    session_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(MentorSession)
        .options(selectinload(MentorSession.mentor))
        .where(MentorSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)

    date_str = utc_to_local(session.sign_in_time).strftime("%b %d %I:%M %p")
    hours = f"{session.hours_counted:.2f}h" if session.hours_counted is not None else "open, no hours"
    await audit.record(
        db, request, "mentor_session.delete",
        f"Deleted {session.mentor.name}'s mentor session ({date_str}, {hours})",
        entity_type="mentor_session", entity_id=session_id,
    )
    await db.execute(delete(MentorSession).where(MentorSession.id == session_id))
    await db.commit()
    return RedirectResponse("/admin/sessions?person_type=mentor", status_code=303)


# ── Settings ───────────────────────────────────────────────────────────────────

def _settings_context() -> dict:
    """Common settings values for the settings template, from the live singleton."""
    return {
        "auto_signout_time": settings.auto_signout_time,
        "weekly_dm_day": settings.weekly_dm_day,
        "weekly_dm_time": settings.weekly_dm_time,
        "signin_ip_whitelist": settings.signin_ip_whitelist,
        "timezone": settings.timezone,
        "backup_time": settings.backup_time,
        "backup_keep": settings.backup_keep,
        "updates_enabled": settings.updates_enabled,
        "roast_enabled": settings.roast_enabled,
        "slack_announce_channel": settings.slack_announce_channel,
        "contributor_multiplier": settings.contributor_multiplier,
        "present_multiplier": settings.present_multiplier,
        "distraction_multiplier": settings.distraction_multiplier,
    }


@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_get(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    from app.services.app_settings import get_leaderboard_since, get_auto_signout_effective_time
    leaderboard_since = await get_leaderboard_since(db)
    auto_signout_effective = await get_auto_signout_effective_time(db)

    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            **_settings_context(),
            "auto_signout_effective": auto_signout_effective,
            "leaderboard_since": leaderboard_since,
        },
    )


ENV_PATH = ".env"


def _write_env(updates: dict[str, str]) -> None:
    """Upsert KEY=value pairs into .env, preserving other lines."""
    try:
        with open(ENV_PATH, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    written: set[str] = set()
    new_lines = []
    for line in lines:
        key = line.split("=", 1)[0].strip().upper()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            written.add(key)
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)


def _update_env_multipliers(
    contributor: float,
    present: float,
    distraction: float,
) -> None:
    """Write multiplier values into .env and update the live settings object."""
    _write_env({
        "CONTRIBUTOR_MULTIPLIER": str(contributor),
        "PRESENT_MULTIPLIER": str(present),
        "DISTRACTION_MULTIPLIER": str(distraction),
    })

    # Update the live singleton so changes take effect immediately
    settings.contributor_multiplier = contributor
    settings.present_multiplier = present
    settings.distraction_multiplier = distraction


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.post("/settings", response_class=HTMLResponse)
async def admin_settings_post(
    request: Request,
    contributor_multiplier: float = Form(...),
    present_multiplier: float = Form(...),
    distraction_multiplier: float = Form(...),
    auto_signout_time: str = Form(...),
    auto_signout_effective: str = Form(""),
    weekly_dm_day: int = Form(...),
    weekly_dm_time: str = Form(...),
    backup_time: str = Form(...),
    backup_keep: int = Form(...),
    timezone: str = Form(...),
    signin_ip_whitelist: str = Form(""),
    slack_announce_channel: str = Form(""),
    updates_enabled: bool = Form(False),
    roast_enabled: bool = Form(False),
    leaderboard_since: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    _update_env_multipliers(
        contributor_multiplier,
        present_multiplier,
        distraction_multiplier,
    )

    # ── General settings ─ validate each field; apply only the valid ones and
    # collect messages for the rest. Changed values are written to .env once,
    # mirrored onto the live singleton, and the scheduler is re-applied.
    errors: list[str] = []
    env_updates: dict[str, str] = {}

    trigger = auto_signout_time.strip()
    if not _HHMM_RE.match(trigger):
        errors.append("Auto sign-out time must be in HH:MM format.")
    elif trigger != settings.auto_signout_time:
        env_updates["AUTO_SIGNOUT_TIME"] = trigger
        settings.auto_signout_time = trigger

    if not 0 <= weekly_dm_day <= 6:
        errors.append("Weekly DM day must be between 0 (Mon) and 6 (Sun).")
    elif weekly_dm_day != settings.weekly_dm_day:
        env_updates["WEEKLY_DM_DAY"] = str(weekly_dm_day)
        settings.weekly_dm_day = weekly_dm_day

    wt = weekly_dm_time.strip()
    if not _HHMM_RE.match(wt):
        errors.append("Weekly DM time must be in HH:MM format.")
    elif wt != settings.weekly_dm_time:
        env_updates["WEEKLY_DM_TIME"] = wt
        settings.weekly_dm_time = wt

    bt = backup_time.strip()
    if not _HHMM_RE.match(bt):
        errors.append("Backup time must be in HH:MM format.")
    elif bt != settings.backup_time:
        env_updates["BACKUP_TIME"] = bt
        settings.backup_time = bt

    if backup_keep < 1:
        errors.append("Backups to keep must be at least 1.")
    elif backup_keep != settings.backup_keep:
        env_updates["BACKUP_KEEP"] = str(backup_keep)
        settings.backup_keep = backup_keep

    tz = timezone.strip()
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"Unknown timezone: {tz!r}.")
    else:
        if tz != settings.timezone:
            env_updates["TIMEZONE"] = tz
            settings.timezone = tz

    wl = signin_ip_whitelist.strip()
    if wl != settings.signin_ip_whitelist:
        env_updates["SIGNIN_IP_WHITELIST"] = wl
        settings.signin_ip_whitelist = wl

    ch = slack_announce_channel.strip()
    if ch != settings.slack_announce_channel:
        env_updates["SLACK_ANNOUNCE_CHANNEL"] = ch
        settings.slack_announce_channel = ch

    if updates_enabled != settings.updates_enabled:
        env_updates["UPDATES_ENABLED"] = "true" if updates_enabled else "false"
        settings.updates_enabled = updates_enabled

    if roast_enabled != settings.roast_enabled:
        env_updates["ROAST_ENABLED"] = "true" if roast_enabled else "false"
        settings.roast_enabled = roast_enabled

    if env_updates:
        _write_env(env_updates)
        from app.services.scheduler import reschedule_all
        reschedule_all(getattr(request.app.state, "scheduler", None))

    # Effective sign-out time — HH:MM override, or blank to clear.
    from app.services.app_settings import (
        set_leaderboard_since, get_leaderboard_since,
        set_auto_signout_effective_time, get_auto_signout_effective_time,
    )
    effective = auto_signout_effective.strip()
    if effective and _HHMM_RE.match(effective):
        await set_auto_signout_effective_time(db, effective)
    elif not effective:
        await set_auto_signout_effective_time(db, None)
    current_effective = await get_auto_signout_effective_time(db)

    parsed: Optional[date] = None
    if leaderboard_since.strip():
        try:
            parsed = date.fromisoformat(leaderboard_since.strip())
        except ValueError:
            parsed = None
    await set_leaderboard_since(db, parsed)
    current_since = await get_leaderboard_since(db)

    await audit.record(
        db, request, "settings.update",
        f"Updated multipliers (contributor={contributor_multiplier}, "
        f"present={present_multiplier}, distraction={distraction_multiplier}); "
        f"auto_signout_time={settings.auto_signout_time}; "
        f"auto_signout_effective={current_effective or 'trigger time'}; "
        f"weekly_dm={settings.weekly_dm_day}@{settings.weekly_dm_time}; "
        f"backup={settings.backup_time} keep={settings.backup_keep}; "
        f"timezone={settings.timezone}; updates_enabled={settings.updates_enabled}; "
        f"roast_enabled={settings.roast_enabled}; "
        f"leaderboard_since={current_since or 'all-time'}",
        entity_type="settings",
        detail={"contributor_multiplier": contributor_multiplier,
                "present_multiplier": present_multiplier,
                "distraction_multiplier": distraction_multiplier,
                "auto_signout_time": settings.auto_signout_time,
                "auto_signout_effective": current_effective,
                "weekly_dm_day": settings.weekly_dm_day,
                "weekly_dm_time": settings.weekly_dm_time,
                "backup_time": settings.backup_time,
                "backup_keep": settings.backup_keep,
                "timezone": settings.timezone,
                "signin_ip_whitelist": settings.signin_ip_whitelist,
                "slack_announce_channel": settings.slack_announce_channel,
                "updates_enabled": settings.updates_enabled,
                "roast_enabled": settings.roast_enabled,
                "leaderboard_since": str(current_since) if current_since else None},
    )
    await db.commit()

    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            **_settings_context(),
            "auto_signout_effective": current_effective,
            "leaderboard_since": current_since,
            "saved": not errors,
            "error": " ".join(errors) if errors else None,
        },
    )


@router.post("/leaderboard/reset")
async def admin_leaderboard_reset(request: Request, db: AsyncSession = Depends(get_db)):
    """Reset the leaderboard to count from today onward. Non-destructive — no
    sessions are deleted, only the cutoff date the totals count from changes."""
    if redirect := _require_auth(request):
        return redirect

    from app.services.app_settings import set_leaderboard_since
    await set_leaderboard_since(db, today_local())
    await audit.record(
        db, request, "leaderboard.reset",
        f"Reset leaderboard cutoff to {today_local()}", entity_type="settings",
    )
    await db.commit()
    return RedirectResponse("/admin", status_code=303)


# ── CSV Import ─────────────────────────────────────────────────────────────────

# CSV roster import was removed — the roster is owned by Legion and pulled in via
# services/legion_sync.py (see the /admin/roster "Sync now" action). Manage members in
# Legion's /admin, not here.


# ── Audit log ──────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(
    request: Request, page: int = 1, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    page = max(page, 1)
    per_page = 50
    total = await db.scalar(select(func.count()).select_from(AuditLog)) or 0
    result = await db.execute(
        select(AuditLog)
        .order_by(AuditLog.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    entries = result.scalars().all()
    total_pages = max((total + per_page - 1) // per_page, 1)

    return templates.TemplateResponse(
        "admin/audit.html",
        {
            "request": request,
            "entries": entries,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


# ── Backup / Restore ─────────────────────────────────────────────────────────

@router.get("/backup", response_class=HTMLResponse)
async def admin_backup_get(request: Request):
    if redirect := _require_auth(request):
        return redirect

    from app.services import backup
    return templates.TemplateResponse(
        "admin/backup.html",
        {
            "request": request,
            "is_sqlite": backup.is_sqlite(),
            "backups": backup.list_backups(),
            "result": request.query_params.get("result"),
            "message": request.query_params.get("message"),
        },
    )


@router.get("/backup/download")
async def admin_backup_download(request: Request):
    if redirect := _require_auth(request):
        return redirect

    from app.services import backup
    if not backup.is_sqlite():
        return RedirectResponse(
            "/admin/backup?result=error&message=Not+a+SQLite+database", status_code=303
        )

    tmp = os.path.join(tempfile.gettempdir(), f"tracker-snapshot-{os.getpid()}.db")
    backup.create_snapshot(tmp)
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)

    filename = f"tracker-backup-{datetime.now():%Y%m%d-%H%M}.db"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/restore")
async def admin_backup_restore(
    request: Request,
    file: UploadFile = File(...),
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services import backup
    if confirm.strip().upper() != "RESTORE":
        return RedirectResponse(
            "/admin/backup?result=error&message=Type+RESTORE+to+confirm", status_code=303
        )

    contents = await file.read()
    ok, message = backup.stage_restore(contents)
    if ok:
        await audit.record(
            db, request, "backup.restore_staged",
            f"Staged restore from uploaded file {file.filename}", entity_type="backup",
        )
        await db.commit()
    result = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/backup?result={result}&message={message.replace(' ', '+')}",
        status_code=303,
    )


# ── Report ─────────────────────────────────────────────────────────────────────

@router.get("/report", response_class=HTMLResponse)
async def admin_report(
    request: Request,
    db: AsyncSession = Depends(get_db),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    team_id: Optional[str] = None,
    category: Optional[str] = None,
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.reports import week_starts_in_range, weekly_attendance_report

    today = today_local()
    default_from = today - timedelta(days=today.weekday()) - timedelta(weeks=3)
    default_to = today - timedelta(days=today.weekday())

    d_from = date_from or default_from
    d_to = date_to or default_to

    team_id_int = int(team_id) if team_id else None
    subteam_slug = category.strip() if category and category.strip() else None

    week_starts = week_starts_in_range(d_from, d_to)
    rows = await weekly_attendance_report(db, week_starts, team_id=team_id_int, subteam_slug=subteam_slug)

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    return templates.TemplateResponse(
        "admin/report.html",
        {
            "request": request,
            "rows": rows,
            "week_starts": week_starts,
            "teams": teams,
            "subteams": await _active_subteams(db),
            "filters": {
                "date_from": d_from,
                "date_to": d_to,
                "team_id": team_id_int,
                "category": category or "",
            },
        },
    )


@router.get("/report/export")
async def admin_report_export(
    request: Request,
    db: AsyncSession = Depends(get_db),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    team_id: Optional[str] = None,
    category: Optional[str] = None,
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.reports import week_starts_in_range, weekly_attendance_report

    today = today_local()
    default_from = today - timedelta(days=today.weekday()) - timedelta(weeks=3)
    default_to = today - timedelta(days=today.weekday())

    d_from = date_from or default_from
    d_to = date_to or default_to

    team_id_int = int(team_id) if team_id else None
    subteam_slug = category.strip() if category and category.strip() else None

    week_starts = week_starts_in_range(d_from, d_to)
    rows = await weekly_attendance_report(db, week_starts, team_id=team_id_int, subteam_slug=subteam_slug)

    def _generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        header = ["Student", "Team", "Subteam"] + [ws.strftime("%Y-%m-%d") for ws in week_starts] + ["Total Hours", "Weeks Met"]
        writer.writerow(header)
        yield buf.getvalue()
        for row in rows:
            buf.seek(0)
            buf.truncate()
            s = row["student"]
            data = [s.name, s.team.number if s.team else "", s.subteam_slug or ""]
            data += [f"{w['hours']:.2f}" for w in row["weeks"]]
            data += [f"{row['total_hours']:.2f}", f"{row['weeks_met']}/{row['weeks_total']}"]
            writer.writerow(data)
            yield buf.getvalue()

    filename = f"weekly_report_{d_from}_{d_to}.csv"
    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
