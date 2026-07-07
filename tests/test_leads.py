"""Lead resolution — a mentor leads a student when they hold the student's
`tempus-lead-<team>-<subteam>` Legion group (services/leads.py)."""
import json

import pytest

from app.models import Mentor
from app.services.leads import lead_mentors_for_student

pytestmark = pytest.mark.asyncio


async def _mentor(db, name, slack, groups, team_id=None, subteam="software", active=True):
    m = Mentor(
        name=name, slack_user_id=slack, member_code=slack,
        team_id=team_id, subteam_slug=subteam, is_active=active,
        group_slugs=json.dumps(groups),
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def test_lead_matched_by_group(db, team, make_student):
    student = await make_student(subteam_slug="software")  # team 4143
    await _mentor(db, "Lead", "U1", ["tempus-lead-4143-software"])
    await _mentor(db, "WrongScope", "U2", ["tempus-lead-4423-design"])
    await _mentor(db, "NoGroups", "U3", [])

    leads = await lead_mentors_for_student(db, student)
    assert [m.name for m in leads] == ["Lead"]


async def test_no_leads_when_student_has_no_subteam(db, team, make_student):
    student = await make_student(subteam_slug=None)
    await _mentor(db, "Lead", "U1", ["tempus-lead-4143-software"])
    assert await lead_mentors_for_student(db, student) == []


async def test_inactive_lead_excluded(db, team, make_student):
    student = await make_student(subteam_slug="software")
    await _mentor(db, "Lead", "U1", ["tempus-lead-4143-software"], active=False)
    assert await lead_mentors_for_student(db, student) == []


async def test_mentor_can_lead_a_subteam_they_are_not_on(db, team, make_student):
    # The group grants lead scope regardless of the mentor's own subteam.
    student = await make_student(subteam_slug="software")
    await _mentor(db, "DesignPerson", "U1", ["tempus-lead-4143-software"], subteam="design")
    leads = await lead_mentors_for_student(db, student)
    assert [m.name for m in leads] == ["DesignPerson"]


async def test_mentor_leads_method():
    m = Mentor(name="x", slack_user_id="U", group_slugs=json.dumps(["tempus-lead-4143-software"]))
    assert m.leads(4143, "software") is True
    assert m.leads(4423, "software") is False
    assert m.leads(4143, None) is False
    assert m.leads(None, "software") is False
    assert Mentor(name="y", slack_user_id="V").leads(4143, "software") is False
