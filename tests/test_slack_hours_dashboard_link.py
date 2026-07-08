"""The /hours reply now ends with a one-tap "open my dashboard" link (to /enter),
mirroring Munus's /vhours. Present for students and mentors with a member_code; omitted
for the not-linked case."""
from datetime import datetime, timedelta

import app.routers.slack as slack_router
from app.models import AttendanceSession, Mentor, MentorSession


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


async def _add_session(db, student_id, hours):
    now = datetime.utcnow()
    db.add(AttendanceSession(
        student_id=student_id, sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now, hours_counted=hours,
    ))
    await db.commit()


async def test_student_hours_includes_dashboard_link(client, db, team, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    student = await make_student(name="Ada Lovelace", code="ada00001", team_id=team.id)
    student.slack_user_id = "USTU"
    await db.commit()
    await _add_session(db, student.id, hours=2.0)

    resp = await client.post("/slack/command", data={"command": "/hours", "user_id": "USTU"})

    assert resp.status_code == 200
    assert "/enter?member=ada00001" in resp.text
    assert "Open my dashboard" in resp.text


async def test_mentor_hours_includes_dashboard_link(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    mentor = Mentor(name="Coach Ray", member_code="mnt00001", slack_user_id="UMENTOR", is_active=True)
    db.add(mentor)
    await db.commit()
    await db.refresh(mentor)
    db.add(MentorSession(
        mentor_id=mentor.id, sign_in_time=datetime.utcnow() - timedelta(hours=3),
        sign_out_time=datetime.utcnow(), hours_counted=3.0,
    ))
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/hours", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "/enter?member=mnt00001" in resp.text
    assert "Open my dashboard" in resp.text


async def test_unlinked_caller_has_no_dashboard_link(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    resp = await client.post("/slack/command", data={"command": "/hours", "user_id": "UNOBODY"})

    assert resp.status_code == 200
    assert "isn't linked" in resp.text
    assert "Open my dashboard" not in resp.text
