"""
Tests that only *lead* mentors (matching the student's team + category) are
looped into the behind-student group DM sent by `notify_student_hours`.
Network sends are patched to capture which path fired and who was included.
"""
import pytest

from app.models import FocusCategory, Mentor, Student


async def _add_behind_student(db, team_id):
    """A student with no sessions this week is behind the weekly requirement."""
    s = Student(name="Behind Student", student_code="behind01", team_id=team_id,
                category=FocusCategory.software, slack_user_id="USTUDENT",
                is_active=True)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def _add_mentor(db, team_id, slack_id, is_lead):
    m = Mentor(name=f"Mentor {slack_id}", slack_user_id=slack_id, team_id=team_id,
               category=FocusCategory.software, is_lead=is_lead)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


def _patch_sends(monkeypatch):
    calls = {"dm": [], "group": []}

    async def fake_send_dm(slack_user_id, text, blocks=None):
        calls["dm"].append(slack_user_id)
        return "ts"

    async def fake_send_group_dm(user_ids, text, blocks=None):
        calls["group"].append(list(user_ids))
        return "ts"

    import app.services.slack_client as sc
    monkeypatch.setattr(sc, "send_dm", fake_send_dm)
    monkeypatch.setattr(sc, "send_group_dm", fake_send_group_dm)
    return sc, calls


@pytest.mark.asyncio
async def test_non_lead_mentor_excluded(db, team, session_factory, monkeypatch):
    student = await _add_behind_student(db, team.id)
    await _add_mentor(db, team.id, "UNONLEAD", is_lead=False)

    sc, calls = _patch_sends(monkeypatch)
    monkeypatch.setattr("app.database.AsyncSessionLocal", session_factory)

    assert await sc.notify_student_hours(student.id) is True
    # No lead → solo DM to the student, no group DM with any mentor.
    assert calls["group"] == []
    assert calls["dm"] == ["USTUDENT"]


@pytest.mark.asyncio
async def test_lead_mentor_included(db, team, session_factory, monkeypatch):
    student = await _add_behind_student(db, team.id)
    await _add_mentor(db, team.id, "UNONLEAD", is_lead=False)
    await _add_mentor(db, team.id, "ULEAD", is_lead=True)

    sc, calls = _patch_sends(monkeypatch)
    monkeypatch.setattr("app.database.AsyncSessionLocal", session_factory)

    assert await sc.notify_student_hours(student.id) is True
    # Behind + a matching lead → group DM with student + lead only.
    assert len(calls["group"]) == 1
    recipients = calls["group"][0]
    assert "USTUDENT" in recipients
    assert "ULEAD" in recipients
    assert "UNONLEAD" not in recipients
    assert calls["dm"] == []
