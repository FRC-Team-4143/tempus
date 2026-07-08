"""
Personal portal — a signed-in member's own attendance page at `/me`. Open to any
active student or mentor on the roster (matched by Legion's `member_code`), regardless
of `/admin` group membership: shows recent sessions, a total-hours headline, and their
own weekly report table. Mirrors Munus's student portal, but for both roles — unlike
Munus, Tempus's kiosk (`/kiosk`, `/mentor`) is the actual public sign-in surface, so
this page is purely informational (no sign-in/out actions live here).
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import AttendanceSession, Mentor, MentorSession, Student
from app.routers.admin import _ADMIN_GROUP, _MANAGER_GROUP
from app.services.app_settings import get_leaderboard_since, leaderboard_since_utc
from app.services.reports import (
    default_report_range, week_starts_in_range, weekly_attendance_report, weekly_mentor_hours,
)
from app.services.sso import logout_url, make_authorize_url, sso_identity
from app.utils import utc_to_local

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)


def _session_role(request: Request) -> Optional[str]:
    identity = sso_identity(request)
    if identity is None:
        return None
    groups = set(identity.get("groups") or [])
    if _ADMIN_GROUP in groups:
        return "admin"
    if _MANAGER_GROUP in groups:
        return "manager"
    return None


templates.env.globals["session_role"] = _session_role
templates.env.globals["session_identity"] = sso_identity


async def _current_person(request: Request, db: AsyncSession):
    """(identity, student, mentor) — exactly one of student/mentor is set for an active
    local record matched by `member_code`; both None if signed in but not synced/inactive;
    identity itself is None if not signed in at all."""
    identity = sso_identity(request)
    if identity is None:
        return None, None, None

    code = identity.get("member_code")
    student = (
        await db.execute(
            select(Student).options(selectinload(Student.team)).where(
                Student.member_code == code, Student.is_active.is_(True),
            )
        )
    ).scalars().first()
    if student:
        return identity, student, None

    mentor = (
        await db.execute(
            select(Mentor).where(Mentor.member_code == code, Mentor.is_active.is_(True))
        )
    ).scalars().first()
    return identity, None, mentor


@router.get("/me", response_class=HTMLResponse)
async def portal_home(
    request: Request,
    db: AsyncSession = Depends(get_db),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
):
    identity, student, mentor = await _current_person(request, db)

    if identity is None:
        return templates.TemplateResponse(
            "portal/identify.html", {"request": request, "authorize_url": make_authorize_url(request)}
        )
    if student is None and mentor is None:
        return templates.TemplateResponse(
            "portal/identify.html",
            {"request": request, "not_synced": True, "signed_in_name": identity.get("name") or "that account"},
        )

    leaderboard_since = await get_leaderboard_since(db)
    since_utc = await leaderboard_since_utc(db)
    default_from, default_to = default_report_range(leaderboard_since)
    d_from = date_from or default_from
    d_to = date_to or default_to
    week_starts = week_starts_in_range(d_from, d_to)

    if student:
        recent = (
            await db.execute(
                select(AttendanceSession)
                .where(AttendanceSession.student_id == student.id)
                .order_by(AttendanceSession.sign_in_time.desc())
                .limit(5)
            )
        ).scalars().all()

        total_q = select(sqlfunc.coalesce(sqlfunc.sum(AttendanceSession.hours_counted), 0.0)).where(
            AttendanceSession.student_id == student.id,
            AttendanceSession.sign_out_time.is_not(None),
        )
        if since_utc is not None:
            total_q = total_q.where(AttendanceSession.sign_in_time >= since_utc)
        total_hours = float((await db.execute(total_q)).scalar() or 0.0)

        report_rows = await weekly_attendance_report(db, week_starts, student_ids=[student.id])
        report = report_rows[0] if report_rows else None
        person, kind = student, "student"
    else:
        recent = (
            await db.execute(
                select(MentorSession)
                .where(MentorSession.mentor_id == mentor.id)
                .order_by(MentorSession.sign_in_time.desc())
                .limit(5)
            )
        ).scalars().all()

        total_q = select(sqlfunc.coalesce(sqlfunc.sum(MentorSession.hours_counted), 0.0)).where(
            MentorSession.mentor_id == mentor.id,
            MentorSession.sign_out_time.is_not(None),
        )
        if since_utc is not None:
            total_q = total_q.where(MentorSession.sign_in_time >= since_utc)
        total_hours = float((await db.execute(total_q)).scalar() or 0.0)

        report = await weekly_mentor_hours(db, week_starts, mentor.id)
        person, kind = mentor, "mentor"

    return templates.TemplateResponse(
        "portal/home.html",
        {
            "request": request,
            "identity": identity,
            "person": person,
            "kind": kind,
            "recent": recent,
            "total_hours": total_hours,
            "leaderboard_since": leaderboard_since,
            "report": report,
            "week_starts": week_starts,
            "filters": {"date_from": d_from, "date_to": d_to},
        },
    )


@router.get("/me/logout")
async def portal_logout(request: Request):
    return RedirectResponse(logout_url(request, return_to="/me"), status_code=303)
