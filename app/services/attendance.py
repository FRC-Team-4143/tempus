"""
Attendance business logic — sign in / sign out / hour calculation.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Student, AttendanceSession, SessionStatus, Mentor, MentorSession


def _status_multiplier(status: SessionStatus) -> float:
    """Return the hours multiplier for a given session status from config."""
    return {
        SessionStatus.contributor: settings.contributor_multiplier,
        SessionStatus.present: settings.present_multiplier,
        SessionStatus.auto: settings.contributor_multiplier,  # auto = same as contributor
        SessionStatus.distraction: settings.distraction_multiplier,
    }.get(status, settings.contributor_multiplier)


async def get_open_session(db: AsyncSession, student_id: int) -> Optional[AttendanceSession]:
    result = await db.execute(
        select(AttendanceSession)
        .where(
            AttendanceSession.student_id == student_id,
            AttendanceSession.sign_out_time.is_(None),
        )
    )
    return result.scalars().first()


async def sign_in(db: AsyncSession, qr_name: str) -> tuple[bool, str, Optional[Student]]:
    """
    Look up a student by the name encoded in their QR code (case-insensitive).
    Returns (success, message, student).
    """
    from sqlalchemy import func as sqlfunc
    result = await db.execute(
        select(Student)
        .options(selectinload(Student.team))
        .where(
            sqlfunc.lower(Student.name) == qr_name.lower(),
            Student.active.is_(True),
        )
    )
    student = result.scalars().first()

    if not student:
        return False, f"Name '{qr_name}' not found. Please see a mentor.", None

    open_session = await get_open_session(db, student.id)
    if open_session:
        return False, f"{student.name} is already signed in.", None

    session = AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow(),
    )
    db.add(session)
    await db.commit()
    return True, f"Welcome, {student.name}!", student


async def sign_out(
    db: AsyncSession,
    session_id: int,
    status: SessionStatus,
) -> Optional[AttendanceSession]:
    """
    Sign out a session and compute hours_counted.
    Returns the updated session or None if not found / already signed out.
    """
    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student).selectinload(Student.team))
        .where(
            AttendanceSession.id == session_id,
            AttendanceSession.sign_out_time.is_(None),
        )
    )
    session = result.scalars().first()
    if not session:
        return None

    now = datetime.utcnow()
    elapsed_hours = (now - session.sign_in_time).total_seconds() / 3600.0

    hours_counted = round(elapsed_hours * _status_multiplier(status), 4)

    session.sign_out_time = now
    session.status = status
    session.hours_counted = hours_counted
    await db.commit()
    await db.refresh(session)
    return session


async def sign_out_all_open(db: AsyncSession, status: SessionStatus = SessionStatus.auto) -> int:
    """
    Sign out every open session (used by the auto sign-out scheduler).
    Returns the number of sessions closed.
    """
    result = await db.execute(
        select(AttendanceSession).where(AttendanceSession.sign_out_time.is_(None))
    )
    open_sessions = result.scalars().all()

    for s in open_sessions:
        now = datetime.utcnow()
        elapsed_hours = (now - s.sign_in_time).total_seconds() / 3600.0
        s.sign_out_time = now
        s.status = status
        s.hours_counted = round(elapsed_hours * _status_multiplier(status), 4)

    await db.commit()
    return len(open_sessions)


async def get_signed_in_students(db: AsyncSession) -> list[AttendanceSession]:
    result = await db.execute(
        select(AttendanceSession)
        .options(
            selectinload(AttendanceSession.student).selectinload(Student.team)
        )
        .where(AttendanceSession.sign_out_time.is_(None))
        .order_by(AttendanceSession.sign_in_time)
    )
    return result.scalars().all()


# ── Mentor sign-in/out ─────────────────────────────────────────────────────────

async def mentor_sign_in(db: AsyncSession, name: str) -> tuple[bool, str, Optional[Mentor]]:
    """Sign in a mentor by name (case-insensitive). Returns (success, message, mentor)."""
    from sqlalchemy import func as sqlfunc
    result = await db.execute(
        select(Mentor)
        .where(sqlfunc.lower(Mentor.name) == name.lower())
    )
    mentor = result.scalars().first()
    if not mentor:
        return False, f"Mentor '{name}' not found. Please ask an admin to add you.", None

    # Check for open session
    open_result = await db.execute(
        select(MentorSession).where(
            MentorSession.mentor_id == mentor.id,
            MentorSession.sign_out_time.is_(None),
        )
    )
    if open_result.scalars().first():
        return False, f"{mentor.name} is already signed in.", None

    session = MentorSession(mentor_id=mentor.id, sign_in_time=datetime.utcnow())
    db.add(session)
    await db.commit()
    return True, f"Welcome, {mentor.name}!", mentor


async def mentor_sign_out_all_open(db: AsyncSession) -> int:
    """Auto sign-out all open mentor sessions. Returns count closed."""
    result = await db.execute(
        select(MentorSession).where(MentorSession.sign_out_time.is_(None))
    )
    open_sessions = result.scalars().all()
    for s in open_sessions:
        now = datetime.utcnow()
        s.sign_out_time = now
        s.hours_counted = round((now - s.sign_in_time).total_seconds() / 3600.0, 4)
    await db.commit()
    return len(open_sessions)


async def get_signed_in_mentors(db: AsyncSession) -> list[MentorSession]:
    result = await db.execute(
        select(MentorSession)
        .options(selectinload(MentorSession.mentor))
        .where(MentorSession.sign_out_time.is_(None))
        .order_by(MentorSession.sign_in_time)
    )
    return result.scalars().all()
