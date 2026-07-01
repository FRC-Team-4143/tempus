"""
Tests for the `/hours` Slack slash command's mentor branch — mentors should
get their own weekly/season totals + overall rank, with no requirement
metric (requirements only apply to students).
"""
from datetime import datetime, timedelta

import app.routers.slack as slack_router
from app.models import Mentor, MentorSession


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


async def _add_mentor(db, slack_id, name="Mentor"):
    m = Mentor(name=name, slack_user_id=slack_id, is_active=True)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_mentor_session(db, mentor_id, hours, days_ago=0):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(MentorSession(
        mentor_id=mentor_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def _hours(client, user_id):
    return await client.post(
        "/slack/command",
        data={"command": "/hours", "text": "", "user_id": user_id},
    )


async def test_mentor_gets_week_and_season_totals(client, db, monkeypatch):
    _bypass_signature(monkeypatch)

    mentor = await _add_mentor(db, "UMENTOR1")
    await _add_mentor_session(db, mentor.id, hours=3.0, days_ago=0)
    await _add_mentor_session(db, mentor.id, hours=5.0, days_ago=10)

    resp = await _hours(client, "UMENTOR1")

    assert resp.status_code == 200
    text = resp.text
    assert "Your Mentor Hours" in text
    assert "This week: *3.0 hrs*" in text
    assert "Season total: *8.0 hrs*" in text
    assert "hrs needed" not in text
    assert "/" not in text.split("This week:")[1].split("\n")[0]


async def test_mentor_with_no_sessions_gets_zero_totals(client, db, monkeypatch):
    _bypass_signature(monkeypatch)

    await _add_mentor(db, "UMENTOR2")

    resp = await _hours(client, "UMENTOR2")

    assert resp.status_code == 200
    assert "This week: *0.0 hrs*" in resp.text
    assert "Season total: *0.0 hrs*" in resp.text
    assert "Rank: *#1 of 1* overall" in resp.text


async def test_mentor_overall_rank_with_tie(client, db, monkeypatch):
    _bypass_signature(monkeypatch)

    a = await _add_mentor(db, "UA", name="A")
    b = await _add_mentor(db, "UB", name="B")
    c = await _add_mentor(db, "UC", name="C")
    await _add_mentor_session(db, a.id, hours=10.0, days_ago=20)
    await _add_mentor_session(db, b.id, hours=5.0, days_ago=20)
    await _add_mentor_session(db, c.id, hours=5.0, days_ago=20)

    # B and C are tied for hours, both rank #2 of 3 (competition ranking).
    resp = await _hours(client, "UB")
    assert "Rank: *#2 of 3* overall" in resp.text

    resp = await _hours(client, "UC")
    assert "Rank: *#2 of 3* overall" in resp.text

    resp = await _hours(client, "UA")
    assert "Rank: *#1 of 3* overall" in resp.text


async def test_unlinked_user_gets_not_linked_error(client, db, monkeypatch):
    _bypass_signature(monkeypatch)

    resp = await _hours(client, "UNOBODY")

    assert resp.status_code == 200
    assert "isn't linked to a student or mentor record" in resp.text


async def test_student_hours_unaffected_by_mentor_branch(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)

    student = await make_student(code="stu00001")
    student.slack_user_id = "USTUDENT"
    db.add(student)
    await db.commit()

    resp = await _hours(client, "USTUDENT")

    assert resp.status_code == 200
    assert "Your Hours" in resp.text
    assert "Your Mentor Hours" not in resp.text
