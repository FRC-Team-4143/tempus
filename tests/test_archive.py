"""Tests for student/mentor archiving (soft delete)."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.models import AttendanceSession, Student
from app.services.attendance import sign_in


async def test_archived_student_cannot_sign_in(db, make_student):
    student = await make_student(code="badge001", is_active=False)

    ok, msg, returned = await sign_in(db, "badge001")

    assert ok is False
    assert returned is None


async def test_active_student_can_still_sign_in(db, make_student):
    await make_student(code="badge001", is_active=True)

    ok, _, returned = await sign_in(db, "badge001")

    assert ok is True
    assert returned is not None


async def test_archive_via_route_preserves_sessions(authed_client, db, make_student):
    student = await make_student(code="badge001")
    # Give them a completed session.
    db.add(AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=2),
        sign_out_time=datetime.utcnow(),
        hours_counted=2.0,
    ))
    await db.commit()

    resp = await authed_client.post(
        f"/admin/students/{student.id}/delete", follow_redirects=False
    )
    assert resp.status_code == 303

    # Student is archived, not deleted, and their session survives.
    refreshed = await db.get(Student, student.id)
    await db.refresh(refreshed)
    assert refreshed.is_active is False
    assert refreshed.archived_at is not None
    session_count = await db.scalar(
        select(func.count()).select_from(AttendanceSession)
        .where(AttendanceSession.student_id == student.id)
    )
    assert session_count == 1


async def test_restore_via_route(authed_client, db, make_student):
    student = await make_student(code="badge001", is_active=False)

    resp = await authed_client.post(
        f"/admin/students/{student.id}/restore", follow_redirects=False
    )
    assert resp.status_code == 303

    refreshed = await db.get(Student, student.id)
    await db.refresh(refreshed)
    assert refreshed.is_active is True
    assert refreshed.archived_at is None


async def test_purge_blocked_when_student_has_sessions(authed_client, db, make_student):
    student = await make_student(code="badge001", is_active=False)
    db.add(AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=1),
        sign_out_time=datetime.utcnow(),
        hours_counted=1.0,
    ))
    await db.commit()

    resp = await authed_client.post(
        f"/admin/students/{student.id}/purge", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "has_sessions" in resp.headers["location"]
    # Still present.
    assert await db.get(Student, student.id) is not None
