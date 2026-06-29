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


async def sign_in(db: AsyncSession, uid: str) -> tuple[bool, str, Optional[Student]]:
    """
    Look up a student by their tracker UID (student_code on the QR badge).
    Returns (success, message, student).
    """
    result = await db.execute(
        select(Student)
        .options(selectinload(Student.team))
        .where(Student.student_code == uid, Student.is_active.is_(True))
    )
    student = result.scalars().first()

    if not student:
        return False, f"Badge not recognized. Please see a mentor.", None

    open_session = await get_open_session(db, student.id)
    if open_session:
        elapsed_seconds = (datetime.utcnow() - open_session.sign_in_time).total_seconds()
        if elapsed_seconds < 60:
            # Debounce: QR scanner fired twice in quick succession — ignore
            return False, f"Duplicate scan ignored — {student.name} is still signed in.", None
        # Self-checkout: sign them out with auto status
        now = datetime.utcnow()
        elapsed_hours = (now - open_session.sign_in_time).total_seconds() / 3600.0
        open_session.sign_out_time = now
        open_session.status = SessionStatus.contributor
        open_session.hours_counted = round(elapsed_hours * _status_multiplier(SessionStatus.contributor), 4)
        await db.commit()
        return True, f"Goodbye, {student.name}! Signed out.", student

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
    sign_out_time = session.checkout_requested_at or now
    elapsed_hours = (sign_out_time - session.sign_in_time).total_seconds() / 3600.0

    hours_counted = round(elapsed_hours * _status_multiplier(status), 4)

    session.sign_out_time = sign_out_time
    session.status = status
    session.hours_counted = hours_counted
    await db.commit()
    await db.refresh(session)
    return session


async def update_session_status(
    db: AsyncSession,
    session_id: int,
    status: SessionStatus,
) -> Optional[AttendanceSession]:
    """
    Update the contribution status of an already-closed session and recalculate hours.
    Returns the updated session or None if not found.
    """
    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student).selectinload(Student.team))
        .where(AttendanceSession.id == session_id)
    )
    session = result.scalars().first()
    if not session or session.sign_out_time is None:
        return None

    elapsed_hours = (session.sign_out_time - session.sign_in_time).total_seconds() / 3600.0
    session.status = status
    session.hours_counted = round(elapsed_hours * _status_multiplier(status), 4)
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
        .where(
            AttendanceSession.sign_out_time.is_(None),
            AttendanceSession.checkout_requested_at.is_(None),
        )
        .order_by(AttendanceSession.sign_in_time)
    )
    return result.scalars().all()


# ── Mentor sign-in/out ─────────────────────────────────────────────────────────

async def mentor_sign_in(db: AsyncSession, uid: str) -> tuple[bool, str, Optional[Mentor]]:
    """Sign in a mentor by their tracker UID (mentor_code). Returns (success, message, mentor)."""
    result = await db.execute(
        select(Mentor)
        .where(Mentor.mentor_code == uid, Mentor.is_active.is_(True))
    )
    mentor = result.scalars().first()
    if not mentor:
        return False, f"Badge not recognized.", None

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
