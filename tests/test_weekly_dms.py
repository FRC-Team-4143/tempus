"""The scheduled weekly hours-update DM (services/scheduler.job_weekly_dms):
skips students whose team/subteam has no hours requirement this week, since a
"0.0 / 0.0 hrs" update carries no useful information."""
from datetime import date, timedelta

from app.models import WeeklyRequirement
from app.services import scheduler


async def _patch_session(monkeypatch, session_factory):
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)


def _patch_sends(monkeypatch):
    sent = []
    dm_calls = []
    group_calls = []

    async def fake_send_dm(slack_user_id, text, blocks=None, automated=False):
        sent.append(slack_user_id)
        dm_calls.append((slack_user_id, text))
        return "ts"

    async def fake_send_group_dm(user_ids, text, blocks=None, automated=False):
        sent.extend(user_ids)
        group_calls.append((user_ids, text))
        return "ts"

    monkeypatch.setattr(scheduler, "send_dm", fake_send_dm)
    monkeypatch.setattr(scheduler, "send_group_dm", fake_send_group_dm)
    return sent, dm_calls, group_calls


async def test_weekly_dm_skips_student_with_zero_requirement(
    db, session_factory, monkeypatch, make_student
):
    await _patch_session(monkeypatch, session_factory)
    sent, dm_calls, group_calls = _patch_sends(monkeypatch)

    # make_student's factory has no slack_user_id kwarg — set it directly after creation.
    student = await make_student(name="Zero Req", code="zero0001")
    student.slack_user_id = "U_ZERO"
    db.add(student)
    await db.commit()

    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    db.add(WeeklyRequirement(team_id=student.team_id, subteam_slug=None, week_start=week_start, required_hours=0.0))
    await db.commit()

    await scheduler.job_weekly_dms()

    assert sent == []
    assert dm_calls == []
    assert group_calls == []


async def test_weekly_dm_still_sends_when_requirement_is_positive(
    db, session_factory, monkeypatch, make_student
):
    await _patch_session(monkeypatch, session_factory)
    sent, dm_calls, group_calls = _patch_sends(monkeypatch)

    student = await make_student(name="Has Req", code="req00001")
    student.slack_user_id = "U_REQ"
    db.add(student)
    await db.commit()

    # No WeeklyRequirement row -> falls back to the nonzero default, so the DM still sends.
    await scheduler.job_weekly_dms()

    assert sent == ["U_REQ"]
    assert len(dm_calls) == 1
    assert "required this week" in dm_calls[0][1]
