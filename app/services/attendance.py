"""
Attendance business logic — sign in / sign out / hour calculation.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Student, AttendanceSession, SessionStatus, Mentor, MentorSession


def _status_multiplier(status: SessionStatus) -> float:
    """Return the hours multiplier for a given session status from config."""
    return {
        SessionStatus.contributor: settings.contributor_multiplier,
        SessionStatus.present: settings.present_multiplier,
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


async def sign_in(db: AsyncSession, uid: str) -> tuple[bool, str, Optional[Student], bool]:
    """
    Look up a student by the QR-badge UID — Legion `member_code`, or the legacy
    `student_code` for badges minted before the Legion cutover.
    Returns (success, message, student, is_sign_out) — the last flag distinguishes
    which side of the toggle a successful scan landed on (kiosk beep, SignInResponse);
    always False on failure.
    """
    result = await db.execute(
        select(Student)
        .options(selectinload(Student.team))
        .where(
            or_(Student.member_code == uid, Student.student_code == uid),
            Student.is_active.is_(True),
        )
    )
    student = result.scalars().first()

    if not student:
        return False, f"Badge not recognized. Please see a mentor.", None, False

    open_session = await get_open_session(db, student.id)
    if open_session:
        elapsed_seconds = (datetime.utcnow() - open_session.sign_in_time).total_seconds()
        if elapsed_seconds < 60:
            # Debounce: QR scanner fired twice in quick succession — ignore
            return False, f"Duplicate scan ignored — {student.name} is still signed in.", None, False
        # Self-checkout: sign them out with auto status
        now = datetime.utcnow()
        elapsed_hours = (now - open_session.sign_in_time).total_seconds() / 3600.0
        open_session.sign_out_time = now
        open_session.status = SessionStatus.contributor
        open_session.hours_counted = round(elapsed_hours * _status_multiplier(SessionStatus.contributor), 4)
        await db.commit()
        return True, f"Goodbye, {student.name}! Signed out.", student, True

    session = AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow(),
    )
    db.add(session)
    await db.commit()
    return True, f"Welcome, {student.name}!", student, False


async def sign_out(
    db: AsyncSession,
    session_id: int,
    status: SessionStatus,
    auto_closed: bool = False,
) -> Optional[AttendanceSession]:
    """
    Sign out a session and compute hours_counted.
    Returns the updated session or None if not found / already signed out.

    ``auto_closed`` marks that something other than the student's own badge scan
    ended this session (e.g. an admin's force-signout button) — see the field's
    docstring on the model.
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
    session.auto_closed = auto_closed
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


async def sign_out_all_open(
    db: AsyncSession,
    status: SessionStatus = SessionStatus.contributor,
    session_length: Optional[timedelta] = None,
) -> list[AttendanceSession]:
    """
    Sign out every open session (used by the nightly auto sign-out job and /gtfo's
    mass sign-out). Returns the list of sessions that were closed, with each
    session's student (and team) eager-loaded so callers know who forgot to sign out.

    Every session this closes is, by definition, one the student didn't end
    themselves — so `auto_closed` is always set, regardless of `status`.

    If ``session_length`` is given, each forgotten session is credited for that
    fixed duration from its own sign_in_time (e.g. a 5-minute nominal session),
    capped at now so a very recent sign-in never gets a future sign-out time.
    Without it, sessions are closed at now.
    """
    result = await db.execute(
        select(AttendanceSession)
        .options(selectinload(AttendanceSession.student).selectinload(Student.team))
        .where(AttendanceSession.sign_out_time.is_(None))
    )
    open_sessions = result.scalars().all()

    for s in open_sessions:
        now = datetime.utcnow()
        sign_out = min(s.sign_in_time + session_length, now) if session_length is not None else now
        elapsed_hours = (sign_out - s.sign_in_time).total_seconds() / 3600.0
        s.sign_out_time = sign_out
        s.status = status
        s.hours_counted = round(elapsed_hours * _status_multiplier(status), 4)
        s.auto_closed = True

    await db.commit()
    return list(open_sessions)


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

async def mentor_sign_in(db: AsyncSession, uid: str) -> tuple[bool, str, Optional[Mentor], bool]:
    """Sign in a mentor by their badge UID — Legion `member_code`, or the legacy
    `mentor_code`. Returns (success, message, mentor, is_sign_out) — see sign_in's
    docstring for what the last flag means."""
    result = await db.execute(
        select(Mentor)
        .where(
            or_(Mentor.member_code == uid, Mentor.mentor_code == uid),
            Mentor.is_active.is_(True),
        )
    )
    mentor = result.scalars().first()
    if not mentor:
        return False, f"Badge not recognized.", None, False

    # Check for open session — badging again toggles to sign-out
    open_result = await db.execute(
        select(MentorSession).where(
            MentorSession.mentor_id == mentor.id,
            MentorSession.sign_out_time.is_(None),
        )
    )
    open_session = open_result.scalars().first()
    if open_session:
        elapsed_seconds = (datetime.utcnow() - open_session.sign_in_time).total_seconds()
        if elapsed_seconds < 60:
            # Debounce: QR scanner fired twice in quick succession — ignore
            return False, f"Duplicate scan ignored — {mentor.name} is still signed in.", None, False
        # Self-checkout
        now = datetime.utcnow()
        open_session.sign_out_time = now
        open_session.hours_counted = round((now - open_session.sign_in_time).total_seconds() / 3600.0, 4)
        await db.commit()
        return True, f"Goodbye, {mentor.name}! Signed out.", mentor, True

    session = MentorSession(mentor_id=mentor.id, sign_in_time=datetime.utcnow())
    db.add(session)
    await db.commit()
    return True, f"Welcome, {mentor.name}!", mentor, False


async def mentor_sign_out_all_open(
    db: AsyncSession, session_length: Optional[timedelta] = None
) -> int:
    """Auto sign-out all open mentor sessions. Returns count closed.

    ``session_length`` behaves as in :func:`sign_out_all_open`. Every session this
    closes is, by definition, one the mentor didn't end themselves, so
    `auto_closed` is always set — see :func:`sign_out_all_open`.
    """
    result = await db.execute(
        select(MentorSession).where(MentorSession.sign_out_time.is_(None))
    )
    open_sessions = result.scalars().all()
    for s in open_sessions:
        now = datetime.utcnow()
        sign_out = min(s.sign_in_time + session_length, now) if session_length is not None else now
        s.sign_out_time = sign_out
        s.hours_counted = round((sign_out - s.sign_in_time).total_seconds() / 3600.0, 4)
        s.auto_closed = True
    await db.commit()
    return len(open_sessions)


async def mentor_sign_out(
    db: AsyncSession, session_id: int, auto_closed: bool = False
) -> Optional[MentorSession]:
    """Sign out a single open mentor session and compute hours_counted.

    Mentor hours are counted in full (no status multiplier). Returns the
    updated session, or None if not found or already signed out.

    ``auto_closed`` behaves as in :func:`sign_out` — true for an admin's
    force-signout button, false for the mentor's own badge scan.
    """
    result = await db.execute(
        select(MentorSession).where(
            MentorSession.id == session_id,
            MentorSession.sign_out_time.is_(None),
        )
    )
    session = result.scalars().first()
    if not session:
        return None

    now = datetime.utcnow()
    session.sign_out_time = now
    session.hours_counted = round((now - session.sign_in_time).total_seconds() / 3600.0, 4)
    session.auto_closed = auto_closed
    await db.commit()
    await db.refresh(session)
    return session


async def get_signed_in_mentors(db: AsyncSession) -> list[MentorSession]:
    result = await db.execute(
        select(MentorSession)
        .options(selectinload(MentorSession.mentor).selectinload(Mentor.team))
        .where(MentorSession.sign_out_time.is_(None))
        .order_by(MentorSession.sign_in_time)
    )
    return result.scalars().all()
