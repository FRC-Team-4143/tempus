"""
Admin routes — password-protected web UI.

Auth: session cookie signed with itsdangerous.
"""
import csv
import hashlib
import hmac
import io
import os
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    AttendanceSession, AuditLog, FocusCategory, Mentor, MentorSession, SessionStatus, Student, Team, WeeklyRequirement,
)
from app.services import audit
from app.utils import utc_to_local, today_local, local_to_utc

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)

_signer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
_COOKIE = "admin_session"
_MAX_AGE = 60 * 60 * 12  # 12 hours


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(_COOKIE)
    if not token:
        return False
    try:
        _signer.loads(token, max_age=_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(request: Request) -> None | RedirectResponse:
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def _make_session_cookie() -> str:
    return _signer.dumps("authenticated")


# ── Login / logout ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, error: str = ""):
    return templates.TemplateResponse(
        "admin/login.html", {"request": request, "error": error}
    )


@router.post("/login")
async def admin_login_post(
    request: Request,
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(password, settings.admin_password):
        await audit.record(db, request, "admin.login_failed", "Failed admin login attempt", actor="anonymous")
        await db.commit()
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    await audit.record(db, request, "admin.login", "Admin signed in")
    await db.commit()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        _COOKIE,
        _make_session_cookie(),
        httponly=True,
        samesite="lax",
        max_age=_MAX_AGE,
    )
    return response


@router.get("/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(_COOKIE)
    return response


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

@router.get("/students", response_class=HTMLResponse)
async def admin_students_list(
    request: Request, show_archived: int = 0, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    student_q = select(Student).options(selectinload(Student.team)).order_by(Student.name)
    if not show_archived:
        student_q = student_q.where(Student.is_active.is_(True))
    result = await db.execute(student_q)
    students = result.scalars().all()

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    return templates.TemplateResponse(
        "admin/students.html",
        {
            "request": request,
            "students": students,
            "teams": teams,
            "categories": list(FocusCategory),
            "show_archived": bool(show_archived),
        },
    )


@router.post("/students")
async def admin_students_create(
    request: Request,
    name: str = Form(...),
    team_id: int = Form(...),
    category: Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    if not category:
        teams_result = await db.execute(select(Team).order_by(Team.number))
        students_result = await db.execute(
            select(Student).options(selectinload(Student.team)).order_by(Student.name)
        )
        return templates.TemplateResponse(
            "admin/students.html",
            {
                "request": request,
                "students": students_result.scalars().all(),
                "teams": teams_result.scalars().all(),
                "categories": list(FocusCategory),
                "error": "Category is required.",
            },
        )

    student = Student(
        name=name.strip(),
        student_code=hashlib.sha256(name.strip().lower().encode()).hexdigest()[:8],
        team_id=team_id,
        category=FocusCategory(category),
        slack_user_id=slack_user_id.strip() if slack_user_id else None,
    )
    db.add(student)
    await audit.record(
        db, request, "student.create", f"Created student {student.name}",
        entity_type="student",
    )
    await db.commit()
    return RedirectResponse("/admin/students", status_code=303)


@router.get("/students/{student_id}/edit", response_class=HTMLResponse)
async def admin_students_edit_get(
    student_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(Student).options(selectinload(Student.team)).where(Student.id == student_id)
    )
    student = result.scalars().first()
    if not student:
        return RedirectResponse("/admin/students", status_code=303)

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    return templates.TemplateResponse(
        "admin/student_edit.html",
        {"request": request, "student": student, "teams": teams, "categories": list(FocusCategory)},
    )


@router.post("/students/{student_id}/edit")
async def admin_students_edit_post(
    student_id: int,
    request: Request,
    name: str = Form(...),
    team_id: int = Form(...),
    category: Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    if not category:
        s_result = await db.execute(
            select(Student).options(selectinload(Student.team)).where(Student.id == student_id)
        )
        student = s_result.scalars().first()
        teams_result = await db.execute(select(Team).order_by(Team.number))
        return templates.TemplateResponse(
            "admin/student_edit.html",
            {
                "request": request,
                "student": student,
                "teams": teams_result.scalars().all(),
                "categories": list(FocusCategory),
                "error": "Category is required.",
            },
        )

    result = await db.execute(select(Student).where(Student.id == student_id))
    student = result.scalars().first()
    if student:
        before = {
            "name": student.name,
            "team_id": student.team_id,
            "category": student.category.value if student.category else None,
            "slack_user_id": student.slack_user_id,
        }
        student.name = name.strip()
        student.team_id = team_id
        student.category = FocusCategory(category)
        student.slack_user_id = slack_user_id.strip() if slack_user_id else None
        after = {
            "name": student.name,
            "team_id": student.team_id,
            "category": student.category.value if student.category else None,
            "slack_user_id": student.slack_user_id,
        }
        await audit.record(
            db, request, "student.edit", f"Edited student {student.name}",
            entity_type="student", entity_id=student.id,
            detail={"before": before, "after": after},
        )
        await db.commit()
    return RedirectResponse("/admin/students", status_code=303)


@router.post("/students/{student_id}/delete")
async def admin_students_delete(
    student_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    await db.execute(delete(Student).where(Student.id == student_id))
    await db.commit()
    return RedirectResponse("/admin/students", status_code=303)


@router.post("/students/{student_id}/notify")
async def admin_students_notify(
    student_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.slack_client import notify_student_hours
    background_tasks.add_task(notify_student_hours, student_id)
    return RedirectResponse("/admin/students?notified=1", status_code=303)


@router.post("/students/notify-all")
async def admin_students_notify_all(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services.slack_client import notify_student_hours
    result = await db.execute(
        select(Student).where(
            Student.slack_user_id.is_not(None),
            Student.is_active.is_(True),
        )
    )
    students = result.scalars().all()
    for s in students:
        background_tasks.add_task(notify_student_hours, s.id)
    return RedirectResponse(f"/admin/students?notified={len(students)}", status_code=303)


# ── Mentors ────────────────────────────────────────────────────────────────────

@router.get("/mentors", response_class=HTMLResponse)
async def admin_mentors_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    result = await db.execute(
        select(Mentor).options(selectinload(Mentor.team)).order_by(Mentor.name)
    )
    mentors = result.scalars().all()

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    return templates.TemplateResponse(
        "admin/mentors.html",
        {"request": request, "mentors": mentors, "teams": teams, "categories": list(FocusCategory)},
    )


@router.post("/mentors")
async def admin_mentors_create(
    request: Request,
    name: str = Form(...),
    slack_user_id: str = Form(...),
    team_id: Optional[int] = Form(None),
    category: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    db.add(Mentor(
        name=name.strip(),
        mentor_code=hashlib.sha256(name.strip().lower().encode()).hexdigest()[:8],
        slack_user_id=slack_user_id.strip(),
        team_id=team_id or None,
        category=FocusCategory(category) if category else None,
    ))
    await audit.record(
        db, request, "mentor.create", f"Created mentor {name.strip()}",
        entity_type="mentor",
    )
    await db.commit()
    return RedirectResponse("/admin/mentors", status_code=303)


@router.post("/mentors/{mentor_id}/delete")
async def admin_mentors_delete(
    mentor_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    await db.execute(delete(Mentor).where(Mentor.id == mentor_id))
    await db.commit()
    return RedirectResponse("/admin/mentors", status_code=303)


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

    # Compute per-row "covers until" by grouping entries in each (team_id, category) scope
    from collections import defaultdict
    scope_entries: dict = defaultdict(list)
    for r in sorted(requirements, key=lambda x: x.week_start):
        scope_entries[(r.team_id, r.category)].append(r)

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
            "categories": list(FocusCategory),
            "covers_until": covers_until,
        },
    )


@router.post("/requirements")
async def admin_requirements_create(
    request: Request,
    team_id: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    week_start: date = Form(...),
    required_hours: float = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    # Normalise to Monday
    week_monday = week_start - timedelta(days=week_start.weekday())
    parsed_category = FocusCategory(category) if category else None
    parsed_team_id = int(team_id) if team_id else None  # empty = all teams

    # Upsert: update if exists
    team_clause = (
        WeeklyRequirement.team_id.is_(None) if parsed_team_id is None
        else WeeklyRequirement.team_id == parsed_team_id
    )
    cat_clause = (
        WeeklyRequirement.category.is_(None) if parsed_category is None
        else WeeklyRequirement.category == parsed_category
    )
    existing_result = await db.execute(
        select(WeeklyRequirement).where(
            team_clause,
            cat_clause,
            WeeklyRequirement.week_start == week_monday,
        )
    )
    existing = existing_result.scalars().first()
    if existing:
        existing.required_hours = required_hours
    else:
        db.add(
            WeeklyRequirement(
                team_id=parsed_team_id, category=parsed_category, week_start=week_monday, required_hours=required_hours
            )
        )
    await audit.record(
        db, request, "requirement.set",
        f"Set requirement {required_hours}h for team={parsed_team_id or 'all'} "
        f"category={parsed_category.value if parsed_category else 'all'} week {week_monday}",
        entity_type="requirement",
        detail={"team_id": parsed_team_id,
                "category": parsed_category.value if parsed_category else None,
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

    await audit.record(
        db, request, "requirement.delete", f"Deleted requirement #{req_id}",
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
    try:
        cat = FocusCategory(cat_str) if cat_str else None
    except ValueError:
        cat = None
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
                query = query.where(Mentor.category == cat)
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
                query = query.where(Student.category == cat)
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
            "categories": list(FocusCategory),
            "filters": {
                "person_type": "mentor" if is_mentor else "student",
                "student_id": mid if is_mentor else sid,
                "team_id": tid,
                "category": cat.value if cat else None,
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
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)

    sid = int(student_id) if student_id and student_id.strip().isdigit() else None
    tid = int(team_id) if team_id and team_id.strip().isdigit() else None
    cat_str = category.strip() if category and category.strip() else None
    try:
        cat = FocusCategory(cat_str) if cat_str else None
    except ValueError:
        cat = None
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
            query = query.where(Student.category == cat)
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
        {"request": request, "s": session, "statuses": [s for s in SessionStatus if s != SessionStatus.auto]},
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

    await audit.record(
        db, request, "session.delete", f"Deleted session #{session_id}",
        entity_type="session", entity_id=session_id,
    )
    await db.execute(delete(AttendanceSession).where(AttendanceSession.id == session_id))
    await db.commit()
    return RedirectResponse("/admin/sessions", status_code=303)


# ── Settings ───────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_get(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    from app.services.app_settings import get_leaderboard_since
    leaderboard_since = await get_leaderboard_since(db)

    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "auto_signout_time": settings.auto_signout_time,
            "weekly_dm_day": settings.weekly_dm_day,
            "weekly_dm_time": settings.weekly_dm_time,
            "signin_ip_whitelist": settings.signin_ip_whitelist,
            "timezone": settings.timezone,
            "contributor_multiplier": settings.contributor_multiplier,
            "present_multiplier": settings.present_multiplier,
            "distraction_multiplier": settings.distraction_multiplier,
            "leaderboard_since": leaderboard_since,
        },
    )


def _update_env_multipliers(
    contributor: float,
    present: float,
    distraction: float,
) -> None:
    """Write multiplier values into .env and update the live settings object."""
    updates = {
        "CONTRIBUTOR_MULTIPLIER": str(contributor),
        "PRESENT_MULTIPLIER": str(present),
        "DISTRACTION_MULTIPLIER": str(distraction),
    }
    env_path = ".env"
    try:
        with open(env_path, "r") as f:
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

    with open(env_path, "w") as f:
        f.writelines(new_lines)

    # Update the live singleton so changes take effect immediately
    settings.contributor_multiplier = contributor
    settings.present_multiplier = present
    settings.distraction_multiplier = distraction


@router.post("/settings", response_class=HTMLResponse)
async def admin_settings_post(
    request: Request,
    contributor_multiplier: float = Form(...),
    present_multiplier: float = Form(...),
    distraction_multiplier: float = Form(...),
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

    from app.services.app_settings import set_leaderboard_since, get_leaderboard_since
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
        f"leaderboard_since={current_since or 'all-time'}",
        entity_type="settings",
        detail={"contributor_multiplier": contributor_multiplier,
                "present_multiplier": present_multiplier,
                "distraction_multiplier": distraction_multiplier,
                "leaderboard_since": str(current_since) if current_since else None},
    )
    await db.commit()

    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "auto_signout_time": settings.auto_signout_time,
            "weekly_dm_day": settings.weekly_dm_day,
            "weekly_dm_time": settings.weekly_dm_time,
            "signin_ip_whitelist": settings.signin_ip_whitelist,
            "timezone": settings.timezone,
            "contributor_multiplier": settings.contributor_multiplier,
            "present_multiplier": settings.present_multiplier,
            "distraction_multiplier": settings.distraction_multiplier,
            "leaderboard_since": current_since,
            "saved": True,
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

@router.get("/import", response_class=HTMLResponse)
async def admin_import_get(request: Request):
    if redirect := _require_auth(request):
        return redirect
    return templates.TemplateResponse("admin/import.html", {"request": request})


@router.post("/import", response_class=HTMLResponse)
async def admin_import_post(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    created = []
    updated = []
    errors = []

    # Build team lookup by number
    teams_result = await db.execute(select(Team).order_by(Team.number))
    team_by_number = {t.number: t for t in teams_result.scalars().all()}

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader, start=2):  # row 1 = header
        row_type = (row.get("type") or "").strip().lower()
        name = (row.get("name") or "").strip()
        team_num_str = (row.get("team_number") or "").strip()
        category_str = (row.get("category") or "").strip().lower()
        slack_uid = (row.get("slack_user_id") or "").strip() or None

        if not row_type or not name:
            errors.append({"row": i, "reason": "Missing type or name", "data": dict(row)})
            continue

        if row_type not in ("student", "mentor"):
            errors.append({"row": i, "reason": f"Unknown type '{row_type}'", "data": dict(row)})
            continue

        if row_type == "student":
            # Required: team_number, category
            try:
                team_num = int(team_num_str)
                team = team_by_number.get(team_num)
                if not team:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append({"row": i, "reason": f"Invalid team_number '{team_num_str}'", "data": dict(row)})
                continue

            if category_str not in ("software", "design", "business"):
                errors.append({"row": i, "reason": f"Invalid category '{category_str}'", "data": dict(row)})
                continue

            category = FocusCategory(category_str)
            result = await db.execute(
                select(Student).where(func.lower(Student.name) == name.lower())
            )
            student = result.scalars().first()
            if student:
                student.team_id = team.id
                student.category = category
                student.slack_user_id = slack_uid
                updated.append(name)
            else:
                db.add(Student(
                    name=name,
                    student_code=hashlib.sha256(name.strip().lower().encode()).hexdigest()[:8],
                    team_id=team.id,
                    category=category,
                    slack_user_id=slack_uid,
                ))
                created.append(name)

        else:  # mentor
            team = None
            if team_num_str:
                try:
                    team = team_by_number.get(int(team_num_str))
                except (ValueError, TypeError):
                    pass

            category = FocusCategory(category_str) if category_str in ("software", "design", "business") else None

            result = await db.execute(
                select(Mentor).where(func.lower(Mentor.name) == name.lower())
            )
            mentor = result.scalars().first()
            if mentor:
                mentor.team_id = team.id if team else None
                mentor.category = category
                if slack_uid:
                    mentor.slack_user_id = slack_uid
                updated.append(name)
            else:
                db.add(Mentor(
                    name=name,
                    mentor_code=hashlib.sha256(name.strip().lower().encode()).hexdigest()[:8],
                    slack_user_id=slack_uid or "",
                    team_id=team.id if team else None,
                    category=category,
                ))
                created.append(name)

    if created or updated:
        await audit.record(
            db, request, "import.csv",
            f"CSV import: {len(created)} created, {len(updated)} updated, {len(errors)} error(s)",
            entity_type="import",
            detail={"created": created, "updated": updated,
                    "error_count": len(errors), "filename": file.filename},
        )
    await db.commit()

    return templates.TemplateResponse(
        "admin/import.html",
        {
            "request": request,
            "created": created,
            "updated": updated,
            "errors": errors,
        },
    )


# ── Audit log ──────────────────────────────────────────────────────────────────

        },
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

# ── Backup / Restore ─────────────────────────────────────────────────────────
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

    cat_enum = None
    if category:
        try:
            cat_enum = FocusCategory(category)
        except ValueError:
            pass

    week_starts = week_starts_in_range(d_from, d_to)
    rows = await weekly_attendance_report(db, week_starts, team_id=team_id_int, category=cat_enum)

    teams_result = await db.execute(select(Team).order_by(Team.number))
    teams = teams_result.scalars().all()

    return templates.TemplateResponse(
        "admin/report.html",
        {
            "request": request,
            "rows": rows,
            "week_starts": week_starts,
            "teams": teams,
            "categories": list(FocusCategory),
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

    cat_enum = None
    if category:
        try:
            cat_enum = FocusCategory(category)
        except ValueError:
            pass

    week_starts = week_starts_in_range(d_from, d_to)
    rows = await weekly_attendance_report(db, week_starts, team_id=team_id_int, category=cat_enum)

    def _generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        header = ["Student", "Team", "Category"] + [ws.strftime("%Y-%m-%d") for ws in week_starts] + ["Total Hours", "Weeks Met"]
        writer.writerow(header)
        yield buf.getvalue()
        for row in rows:
            buf.seek(0)
            buf.truncate()
            s = row["student"]
            data = [s.name, s.team.number if s.team else "", s.category.value if s.category else ""]
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
