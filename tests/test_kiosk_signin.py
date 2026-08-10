"""Coverage for POST /kiosk/signin's JSON contract — specifically `is_mentor`,
which the combined kiosk display (kiosk_combined.html) reads to decide whether
*this* browser should swap to the mentor board. The swap itself is browser
timing and stays out of pytest (see test_kiosk_combined.py); what belongs here
is the server's half of that contract.
"""
from datetime import datetime, timedelta

from app.models import AttendanceSession, Mentor, MentorSession


async def test_student_signin_reports_is_mentor_false(paired_client, make_student):
    student = await make_student(code="badge001")

    resp = await paired_client.post("/kiosk/signin", json={"name": "badge001"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["is_mentor"] is False
    assert data["student_name"] == student.name


async def test_student_self_checkout_reports_is_mentor_false(paired_client, db, make_student):
    student = await make_student(code="badge001")
    db.add(AttendanceSession(
        student_id=student.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=2),
    ))
    await db.commit()

    resp = await paired_client.post("/kiosk/signin", json={"name": "badge001"})

    data = resp.json()
    assert data["success"] is True
    assert "Signed out" in data["message"]
    assert data["is_mentor"] is False


async def test_mentor_signin_reports_is_mentor_true(paired_client, db):
    mentor = Mentor(name="Grace Hopper", slack_user_id="U_MENTOR_1", member_code="mbadge001")
    db.add(mentor)
    await db.commit()

    resp = await paired_client.post("/kiosk/signin", json={"name": "mbadge001"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["is_mentor"] is True
    # Mentors never appear on the student board's payload.
    assert data["student_name"] is None


async def test_mentor_self_checkout_reports_is_mentor_true(paired_client, db):
    mentor = Mentor(name="Grace Hopper", slack_user_id="U_MENTOR_1", member_code="mbadge001")
    db.add(mentor)
    await db.commit()
    await db.refresh(mentor)
    db.add(MentorSession(
        mentor_id=mentor.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=1),
    ))
    await db.commit()

    resp = await paired_client.post("/kiosk/signin", json={"name": "mbadge001"})

    data = resp.json()
    assert data["success"] is True
    assert "Signed out" in data["message"]
    assert data["is_mentor"] is True


async def test_unknown_badge_reports_success_false_and_is_mentor_false(paired_client):
    resp = await paired_client.post("/kiosk/signin", json={"name": "nobody"})

    data = resp.json()
    assert data["success"] is False
    assert data["is_mentor"] is False


async def test_unpaired_display_reports_is_mentor_false(client):
    resp = await client.post("/kiosk/signin", json={"name": "whoever"})

    data = resp.json()
    assert data["success"] is False
    assert data["is_mentor"] is False


async def test_debounced_mentor_scan_reports_its_own_message(paired_client, db):
    """Regression test: kiosk_signin()'s final fallback used to return the stale
    student-lookup failure ("Badge not recognized") even when a mentor badge was
    the one that matched but got debounced — discarding the mentor lookup's real
    "Duplicate scan ignored" message."""
    mentor = Mentor(name="Grace Hopper", slack_user_id="U_MENTOR_1", member_code="mbadge001")
    db.add(mentor)
    await db.commit()
    await db.refresh(mentor)
    db.add(MentorSession(mentor_id=mentor.id, sign_in_time=datetime.utcnow()))
    await db.commit()

    resp = await paired_client.post("/kiosk/signin", json={"name": "mbadge001"})

    data = resp.json()
    assert data["success"] is False
    assert "Duplicate scan ignored" in data["message"]
