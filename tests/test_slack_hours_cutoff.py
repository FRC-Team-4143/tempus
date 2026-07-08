"""The /hours Slack command's season total and rank used to be computed on a true
all-time basis, inconsistent with the kiosk/dashboard leaderboards (which already
respect the "counts hours since" cutoff). This confirms /hours now uses the same
cutoff — for both students and mentors — while staying unchanged when no cutoff is
configured (the default)."""
from datetime import date, datetime, timedelta

import app.routers.slack as slack_router
from app.models import AttendanceSession, Mentor, MentorSession
from app.services.app_settings import set_leaderboard_since


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


async def _add_session(db, student_id, hours, days_ago):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def _add_mentor(db, slack_id, name="Coach Ray"):
    m = Mentor(name=name, slack_user_id=slack_id, is_active=True)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_mentor_session(db, mentor_id, hours, days_ago):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(MentorSession(
        mentor_id=mentor_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def test_student_season_total_respects_cutoff(client, db, team, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    student = await make_student(name="Ada Lovelace", code="ada00001", team_id=team.id)
    student.slack_user_id = "USTU"
    await db.commit()

    await _add_session(db, student.id, hours=5.0, days_ago=30)  # before cutoff
    await _add_session(db, student.id, hours=2.0, days_ago=1)   # after cutoff
    await set_leaderboard_since(db, date.today() - timedelta(days=7))

    resp = await client.post("/slack/command", data={"command": "/hours", "user_id": "USTU"})

    assert resp.status_code == 200
    assert "Season total: *2.0 hrs*" in resp.text
    assert "Counting since" in resp.text


async def test_student_season_total_unchanged_when_no_cutoff(client, db, team, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    student = await make_student(name="Ada Lovelace", code="ada00001", team_id=team.id)
    student.slack_user_id = "USTU"
    await db.commit()

    await _add_session(db, student.id, hours=5.0, days_ago=30)
    await _add_session(db, student.id, hours=2.0, days_ago=1)

    resp = await client.post("/slack/command", data={"command": "/hours", "user_id": "USTU"})

    assert resp.status_code == 200
    assert "Season total: *7.0 hrs*" in resp.text
    assert "Counting since" not in resp.text


async def test_mentor_season_total_respects_cutoff(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    mentor = await _add_mentor(db, slack_id="UMENTOR")

    await _add_mentor_session(db, mentor.id, hours=4.0, days_ago=30)  # before cutoff
    await _add_mentor_session(db, mentor.id, hours=1.0, days_ago=1)   # after cutoff
    await set_leaderboard_since(db, date.today() - timedelta(days=7))

    resp = await client.post("/slack/command", data={"command": "/hours", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "Season total: *1.0 hrs*" in resp.text
    assert "Counting since" in resp.text
