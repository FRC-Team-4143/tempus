"""Tests for manually logging a full (forgotten) session for a student or mentor
via the combined "New Session" modal on /admin/sessions, posted to
/admin/sessions/new."""
import json

from sqlalchemy import select

from app.models import AttendanceSession, AuditLog, Mentor, MentorSession
from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie


async def test_new_session_modal_lists_students_and_mentors(authed_client, db, make_student):
    await make_student()
    db.add(Mentor(name="Coach Ray", member_code="mnt00099", slack_user_id="Umnt00099", is_active=True))
    await db.commit()

    resp = await authed_client.get("/admin/sessions")
    assert resp.status_code == 200
    assert 'id="new-session-modal"' in resp.text
    assert "Ada Lovelace" in resp.text
    assert "Coach Ray" in resp.text


async def test_create_student_session_same_day(authed_client, db, make_student):
    student = await make_student(code="badge010")

    resp = await authed_client.post(
        "/admin/sessions/new",
        data={
            "person_id": f"student-{student.id}",
            "session_date": "2026-08-10",
            "start_time": "09:00",
            "end_time": "12:00",
            "status": "contributor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/sessions?person_type=all"

    result = await db.execute(select(AttendanceSession).where(AttendanceSession.student_id == student.id))
    sessions = result.scalars().all()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.sign_out_time is not None
    assert s.status.value == "contributor"
    assert s.hours_counted == 3.0

    audit_rows = (await db.execute(
        select(AuditLog).where(AuditLog.action == "session.create")
    )).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].entity_id == str(s.id)


async def test_create_student_session_past_midnight(authed_client, db, make_student):
    student = await make_student(code="badge011")

    resp = await authed_client.post(
        "/admin/sessions/new",
        data={
            "person_id": f"student-{student.id}",
            "session_date": "2026-08-10",
            "start_time": "22:00",
            "end_time": "01:00",
            "status": "contributor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    result = await db.execute(select(AttendanceSession).where(AttendanceSession.student_id == student.id))
    s = result.scalars().first()
    assert s.hours_counted == 3.0
    assert s.sign_out_time > s.sign_in_time


async def test_create_mentor_session(authed_client, db):
    mentor = Mentor(name="Coach Ray", member_code="mnt00010", slack_user_id="Umnt00010", is_active=True)
    db.add(mentor)
    await db.commit()
    await db.refresh(mentor)

    resp = await authed_client.post(
        "/admin/sessions/new",
        data={
            "person_id": f"mentor-{mentor.id}",
            "session_date": "2026-08-10",
            "start_time": "09:00",
            "end_time": "11:30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/sessions?person_type=all"

    result = await db.execute(select(MentorSession).where(MentorSession.mentor_id == mentor.id))
    sessions = result.scalars().all()
    assert len(sessions) == 1
    assert sessions[0].hours_counted == 2.5

    audit_rows = (await db.execute(
        select(AuditLog).where(AuditLog.action == "mentor_session.create")
    )).scalars().all()
    assert len(audit_rows) == 1


async def test_manager_can_create_session(client, db, make_student):
    student = await make_student(code="badge012")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))

    resp = await client.post(
        "/admin/sessions/new",
        data={
            "person_id": f"student-{student.id}",
            "session_date": "2026-08-10",
            "start_time": "09:00",
            "end_time": "10:00",
            "status": "contributor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    result = await db.execute(select(AttendanceSession).where(AttendanceSession.student_id == student.id))
    assert len(result.scalars().all()) == 1
