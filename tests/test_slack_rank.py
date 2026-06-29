"""
Tests for the leaderboard rank line in the student hours Slack DM
(`notify_student_hours`). Network sends are patched out; we assert on the
message text and verify competition ranking (ties share a rank).
"""
from datetime import datetime, timedelta

import pytest

from app.models import AttendanceSession, Student, Team


async def _add_student(db, name, code, team_id, slack_id):
    s = Student(name=name, student_code=code, team_id=team_id,
                slack_user_id=slack_id, is_active=True)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def _add_session(db, student_id, hours):
    now = datetime.utcnow()
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_rank_line_competition_ranking(db, team, session_factory, monkeypatch):
    # team fixture = 4143; add a second program team 4423
    team2 = Team(number=4423, name="Team 4423")
    db.add(team2)
    await db.commit()
    await db.refresh(team2)

    # Totals: D=20 (4423), A=10, B=5, C=5 (tie) on 4143
    a = await _add_student(db, "A", "aaaa0001", team.id, "UA")
    b = await _add_student(db, "B", "bbbb0001", team.id, "UB")
    c = await _add_student(db, "C", "cccc0001", team.id, "UC")
    d = await _add_student(db, "D", "dddd0001", team2.id, "UD")
    await _add_session(db, a.id, 10.0)
    await _add_session(db, b.id, 5.0)
    await _add_session(db, c.id, 5.0)
    await _add_session(db, d.id, 20.0)

    captured: dict[str, str] = {}

    async def fake_send_dm(slack_user_id, text, blocks=None):
        captured["text"] = text
        return "ts"

    async def fake_send_group_dm(user_ids, text, blocks=None):
        captured["text"] = text
        return "ts"

    import app.services.slack_client as sc
    monkeypatch.setattr(sc, "send_dm", fake_send_dm)
    monkeypatch.setattr(sc, "send_group_dm", fake_send_group_dm)
    monkeypatch.setattr("app.database.AsyncSessionLocal", session_factory)

    # B: overall #3 of 4 (D, A above; tied with C), team #2 of 3 on 4143
    assert await sc.notify_student_hours(b.id) is True
    assert "#3 of 4* overall" in captured["text"]
    assert "#2 of 3* on Team 4143" in captured["text"]

    # C: identical to B by the tie rule
    assert await sc.notify_student_hours(c.id) is True
    assert "#3 of 4* overall" in captured["text"]
    assert "#2 of 3* on Team 4143" in captured["text"]

    # D: top overall, alone on its team
    assert await sc.notify_student_hours(d.id) is True
    assert "#1 of 4* overall" in captured["text"]
    assert "#1 of 1* on Team 4423" in captured["text"]
