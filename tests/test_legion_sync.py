"""Legion roster sync (services/legion_sync.py) — upsert by member_code, back-link
legacy rows, map subteam/groups, skip teamless students, advance the watermark."""
import json

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Mentor, Student, Subteam, Team
from app.services import legion_sync
from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting

pytestmark = pytest.mark.asyncio

TEAMS = [{"number": 4143, "name": "MARS/WARS"}, {"number": 4423, "name": "MARS' Minions"}]
SUBTEAMS = [
    {"slug": "software", "label": "Software", "is_active": True},
    {"slug": "design", "label": "Design", "is_active": True},
]
STUDENT = {
    "member_code": "stu00001", "name": "Ada Student", "role": "student",
    "team_number": 4143, "subteam": {"slug": "software", "label": "Software"},
    "groups": [], "slack_user_id": "USTU", "is_active": True, "grade": "junior",
    "updated_at": "2026-07-01T00:00:00",
}
MENTOR = {
    "member_code": "men00001", "name": "Grace Mentor", "role": "mentor",
    "team_number": 4143, "subteam": {"slug": "software", "label": "Software"},
    "groups": ["tempus-lead-4143-software", "tempus-admin"],
    "slack_user_id": "UMEN", "is_active": True, "grade": None,
    "updated_at": "2026-07-01T00:00:00",
}


@pytest.fixture
def legion_api(monkeypatch):
    """Configure Legion + stub the HTTP layer. Override `members` per-test if needed."""
    monkeypatch.setattr(settings, "legion_base_url", "http://legion.test")
    monkeypatch.setattr(settings, "legion_api_key", "key")

    state = {"members": [STUDENT, MENTOR]}

    async def fake_get(client, path, **params):
        if path == "/api/teams":
            return {"teams": TEAMS}
        if path == "/api/subteams":
            return {"subteams": SUBTEAMS}
        if path == "/api/members":
            return {"members": state["members"]}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(legion_sync, "_get", fake_get)
    return state


async def test_sync_upserts_roster(db, legion_api):
    await legion_sync.sync_roster(db)

    assert (await db.scalar(select(func.count()).select_from(Team))) == 2
    assert (await db.scalar(select(func.count()).select_from(Subteam))) == 2

    student = (await db.execute(select(Student).where(Student.member_code == "stu00001"))).scalar_one()
    assert student.name == "Ada Student"
    assert student.subteam_slug == "software"
    team_4143 = (await db.execute(select(Team).where(Team.number == 4143))).scalar_one()
    assert student.team_id == team_4143.id

    mentor = (await db.execute(select(Mentor).where(Mentor.member_code == "men00001"))).scalar_one()
    assert json.loads(mentor.group_slugs) == ["tempus-lead-4143-software", "tempus-admin"]
    assert mentor.leads(4143, "software") is True


async def test_sync_backlinks_legacy_row_by_slack_id(db, team, legion_api):
    # A pre-existing student created before the cutover (no member_code), same slack id.
    db.add(Student(name="Ada Student", student_code="legacy01", slack_user_id="USTU", team_id=team.id))
    await db.commit()

    await legion_sync.sync_roster(db)

    students = (await db.execute(select(Student).where(Student.slack_user_id == "USTU"))).scalars().all()
    assert len(students) == 1  # linked, not duplicated
    assert students[0].member_code == "stu00001"
    assert students[0].student_code == "legacy01"  # legacy badge preserved


async def test_sync_skips_student_without_team(db, legion_api):
    legion_api["members"] = [dict(STUDENT, team_number=None)]
    summary = await legion_sync.sync_roster(db)
    assert (await db.scalar(select(func.count()).select_from(Student))) == 0
    assert "1 skipped" in summary


async def test_sync_advances_watermark(db, legion_api):
    assert await get_setting(db, LEGION_LAST_SYNCED_KEY) is None
    await legion_sync.sync_roster(db)
    assert await get_setting(db, LEGION_LAST_SYNCED_KEY) is not None


async def test_sync_requires_configuration(db, monkeypatch):
    monkeypatch.setattr(settings, "legion_base_url", "")
    monkeypatch.setattr(settings, "legion_api_key", "")
    with pytest.raises(legion_sync.LegionSyncError):
        await legion_sync.sync_roster(db)
