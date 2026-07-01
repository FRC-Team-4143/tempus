"""Tests for core sign-in/out hour math in app/services/attendance.py."""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models import AttendanceSession, SessionStatus
from app.services import attendance
from app.services.attendance import (
    get_open_session,
    sign_in,
    sign_out,
    sign_out_all_open,
    update_session_status,
)


async def test_sign_in_creates_open_session(db, make_student):
    student = await make_student(code="badge001")

    ok, msg, returned = await sign_in(db, "badge001")

    assert ok is True
    assert returned.id == student.id
    open_session = await get_open_session(db, student.id)
    assert open_session is not None
    assert open_session.sign_out_time is None


async def test_unknown_badge_is_rejected(db, make_student):
    await make_student(code="badge001")

    ok, msg, returned = await sign_in(db, "does-not-exist")

    assert ok is False
    assert returned is None


async def test_second_scan_within_60s_is_debounced(db, make_student):
    student = await make_student(code="badge001")
    # First scan opens a session 10 seconds ago.
    db.add(AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(seconds=10),
    ))
    await db.commit()

    ok, msg, returned = await sign_in(db, "badge001")

    assert ok is False
    assert "Duplicate" in msg
    # Still exactly one, still open.
    open_session = await get_open_session(db, student.id)
    assert open_session is not None
    assert open_session.sign_out_time is None


async def test_second_scan_after_60s_self_checks_out(db, make_student):
    student = await make_student(code="badge001")
    db.add(AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=2),
    ))
    await db.commit()

    ok, msg, returned = await sign_in(db, "badge001")

    assert ok is True
    assert "Signed out" in msg
    assert await get_open_session(db, student.id) is None  # no open session remains
    from sqlalchemy import select
    sess = (await db.execute(select(AttendanceSession))).scalars().first()
    assert sess.status == SessionStatus.contributor
    # 2 hours * contributor multiplier
    assert sess.hours_counted == pytest.approx(2.0 * settings.contributor_multiplier, abs=0.01)


@pytest.mark.parametrize(
    "status,multiplier",
    [
        (SessionStatus.contributor, settings.contributor_multiplier),
        (SessionStatus.present, settings.present_multiplier),
        (SessionStatus.distraction, settings.distraction_multiplier),
        (SessionStatus.auto, settings.contributor_multiplier),  # auto == contributor
    ],
)
async def test_sign_out_applies_status_multiplier(db, make_student, status, multiplier):
    student = await make_student(code="badge001")
    sess = AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=4),
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    result = await sign_out(db, sess.id, status)

    assert result is not None
    assert result.status == status
    assert result.hours_counted == pytest.approx(4.0 * multiplier, abs=0.01)


async def test_distraction_counts_zero_hours(db, make_student):
    student = await make_student(code="badge001")
    sess = AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=3),
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    result = await sign_out(db, sess.id, SessionStatus.distraction)

    assert result.hours_counted == 0.0


async def test_sign_out_already_closed_returns_none(db, make_student):
    student = await make_student(code="badge001")
    sess = AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=1),
        sign_out_time=datetime.utcnow(),
        status=SessionStatus.present,
        hours_counted=0.5,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    assert await sign_out(db, sess.id, SessionStatus.contributor) is None


async def test_update_session_status_recalculates(db, make_student):
    student = await make_student(code="badge001")
    sign_in_time = datetime.utcnow() - timedelta(hours=2)
    sess = AttendanceSession(
        student_id=student.id,
        sign_in_time=sign_in_time,
        sign_out_time=sign_in_time + timedelta(hours=2),
        status=SessionStatus.contributor,
        hours_counted=2.0,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    result = await update_session_status(db, sess.id, SessionStatus.present)

    assert result.status == SessionStatus.present
    assert result.hours_counted == pytest.approx(2.0 * settings.present_multiplier, abs=0.01)


async def test_sign_out_all_open_closes_everything(db, make_student):
    s1 = await make_student(name="A", code="a0000001")
    s2 = await make_student(name="B", code="b0000001")
    for s in (s1, s2):
        db.add(AttendanceSession(
            student_id=s.id,
            sign_in_time=datetime.utcnow() - timedelta(hours=1),
        ))
    await db.commit()

    closed = await sign_out_all_open(db)

    assert len(closed) == 2
    assert {c.student.name for c in closed} == {"A", "B"}
    assert await get_open_session(db, s1.id) is None
    assert await get_open_session(db, s2.id) is None
