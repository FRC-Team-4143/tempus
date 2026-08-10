"""Tests for the /gtfo Slack slash command — mentor-only mass sign-out of every
student and mentor currently signed in."""
from datetime import datetime, timedelta

from sqlalchemy import select

import app.routers.slack as slack_router
from app.models import AttendanceSession, AuditLog, Mentor, MentorSession, SessionStatus


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


def _stub_wall_of_shame(monkeypatch):
    calls = []

    async def _fake(closed):
        calls.append(closed)

    monkeypatch.setattr(slack_router, "_post_wall_of_shame", _fake)
    return calls


async def _add_mentor(db, slack_id, name="Coach Ray", is_active=True, code="mnt00001"):
    m = Mentor(name=name, slack_user_id=slack_id, member_code=code, is_active=is_active)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _open_session(db, student, minutes_ago=30):
    s = AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def _open_mentor_session(db, mentor, minutes_ago=30):
    s = MentorSession(
        mentor_id=mentor.id,
        sign_in_time=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def test_gtfo_requires_mentor(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    _stub_wall_of_shame(monkeypatch)
    student = await make_student(name="Ada Lovelace", code="ada00001")
    student.slack_user_id = "USTU"
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "USTU"})

    assert resp.status_code == 200
    assert "Only registered mentors" in resp.text


async def test_gtfo_rejects_inactive_mentor(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_wall_of_shame(monkeypatch)
    await _add_mentor(db, "UOLDMENTOR", is_active=False)

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "UOLDMENTOR"})

    assert resp.status_code == 200
    assert "Only registered mentors" in resp.text
    assert calls == []


async def test_gtfo_signs_out_all_open_sessions(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_wall_of_shame(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    s1 = await make_student(name="Ada Lovelace", code="ada00001")
    s2 = await make_student(name="Bo Diaz", code="bod00001")
    await _open_session(db, s1)
    await _open_session(db, s2)

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "Signed out 2 student(s)" in resp.text

    db.expire_all()
    rows = (await db.execute(select(AttendanceSession))).scalars().all()
    assert all(r.sign_out_time is not None for r in rows)
    assert all(r.status == SessionStatus.auto for r in rows)

    assert len(calls) == 1
    assert len(calls[0]) == 2


async def test_gtfo_with_no_open_sessions(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_wall_of_shame(monkeypatch)
    await _add_mentor(db, "UMENTOR")

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "No one was signed in" in resp.text
    assert calls == [[]]


async def test_gtfo_also_signs_out_open_mentor_sessions(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_wall_of_shame(monkeypatch)
    caller = await _add_mentor(db, "UMENTOR", name="Coach Ray")
    other = await _add_mentor(db, "UMENTOR2", name="Coach Lee", code="mnt00002")
    await _open_mentor_session(db, caller)
    await _open_mentor_session(db, other)

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "Signed out" in resp.text
    assert "2 mentor(s)" in resp.text

    db.expire_all()
    rows = (await db.execute(select(MentorSession))).scalars().all()
    assert len(rows) == 2
    assert all(r.sign_out_time is not None for r in rows)

    # Mentors don't get roasted — only the (empty) student list reaches the meme poster.
    assert len(calls) == 1
    assert calls[0] == []


async def test_gtfo_reports_students_and_mentors_together(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    _stub_wall_of_shame(monkeypatch)
    caller = await _add_mentor(db, "UMENTOR", name="Coach Ray")
    await _open_mentor_session(db, caller)
    student = await make_student(name="Ada Lovelace", code="ada00001")
    await _open_session(db, student)

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "1 student(s)" in resp.text
    assert "1 mentor(s)" in resp.text


async def test_gtfo_writes_audit_row(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    _stub_wall_of_shame(monkeypatch)
    await _add_mentor(db, "UMENTOR", name="Coach Ray")
    student = await make_student()
    await _open_session(db, student)

    resp = await client.post("/slack/command", data={"command": "/gtfo", "user_id": "UMENTOR"})
    assert resp.status_code == 200

    rows = (await db.execute(
        select(AuditLog).where(AuditLog.action == "attendance.bulk_signout")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor == "Coach Ray"
    assert "Coach Ray signed out 1 student(s) and 0 mentor(s) via /gtfo" in rows[0].summary
