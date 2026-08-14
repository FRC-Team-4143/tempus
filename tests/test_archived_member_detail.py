"""Tests for the member-search detail pages (/admin/report/archived/students/{id},
/admin/report/archived/mentors/{id}) — specifically the date_from/date_to total +
session-history filter, which defaults to all-time when omitted."""
from datetime import datetime, timedelta

import pytest

from app.models import AttendanceSession, Mentor, MentorSession

pytestmark = pytest.mark.asyncio


async def _add_session(db, student_id, *, days_ago, hours=3.0):
    sign_in = datetime.utcnow() - timedelta(days=days_ago, hours=hours)
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=hours),
        hours_counted=hours,
    ))
    await db.commit()


async def _add_mentor(db, name="Coach Ray", is_active=True):
    m = Mentor(name=name, slack_user_id=f"U{name}", is_active=is_active)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_mentor_session(db, mentor_id, *, days_ago, hours=2.0):
    sign_in = datetime.utcnow() - timedelta(days=days_ago, hours=hours)
    db.add(MentorSession(
        mentor_id=mentor_id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=hours),
        hours_counted=hours,
    ))
    await db.commit()


async def test_student_detail_defaults_to_all_time(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_session(db, grad.id, days_ago=400, hours=3.0)
    await _add_session(db, grad.id, days_ago=40, hours=2.0)

    resp = await authed_client.get(f"/admin/report/archived/students/{grad.id}")
    assert resp.status_code == 200
    assert "5.0 hrs" in resp.text
    assert "All-time total" in resp.text


async def test_student_detail_narrows_to_custom_range(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    old = datetime.utcnow() - timedelta(days=400)
    recent = datetime.utcnow() - timedelta(days=40)
    await _add_session(db, grad.id, days_ago=400, hours=3.0)
    await _add_session(db, grad.id, days_ago=40, hours=2.0)

    d_from = (recent - timedelta(days=1)).date().isoformat()
    d_to = (recent + timedelta(days=1)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/archived/students/{grad.id}?date_from={d_from}&date_to={d_to}"
    )
    assert resp.status_code == 200
    assert "2.0 hrs" in resp.text
    assert "3.0 hrs" not in resp.text
    assert "filtered to range" in resp.text


async def test_student_detail_range_excluding_all_sessions_shows_zero(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_session(db, grad.id, days_ago=400, hours=3.0)

    future_from = (datetime.utcnow() + timedelta(days=10)).date().isoformat()
    future_to = (datetime.utcnow() + timedelta(days=20)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/archived/students/{grad.id}?date_from={future_from}&date_to={future_to}"
    )
    assert resp.status_code == 200
    assert "0.0 hrs" in resp.text


async def test_mentor_detail_defaults_to_all_time_then_narrows(authed_client, db):
    old_coach = await _add_mentor(db, "Old Coach", is_active=False)
    await _add_mentor_session(db, old_coach.id, days_ago=400, hours=4.0)
    await _add_mentor_session(db, old_coach.id, days_ago=40, hours=1.0)

    resp = await authed_client.get(f"/admin/report/archived/mentors/{old_coach.id}")
    assert resp.status_code == 200
    assert "5.0 hrs" in resp.text

    recent = datetime.utcnow() - timedelta(days=40)
    d_from = (recent - timedelta(days=1)).date().isoformat()
    d_to = (recent + timedelta(days=1)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/archived/mentors/{old_coach.id}?date_from={d_from}&date_to={d_to}"
    )
    assert resp.status_code == 200
    assert "1.0 hrs" in resp.text
    assert "5.0 hrs" not in resp.text


async def test_active_student_detail_still_shows_weekly_table_and_total(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001", is_active=True)
    await _add_session(db, ada.id, days_ago=1, hours=2.0)

    resp = await authed_client.get(f"/admin/report/archived/students/{ada.id}")
    assert resp.status_code == 200
    assert "Weekly totals" in resp.text
    # The new Total Hours card is additive, not a replacement, for an active person.
    assert "All-time total" in resp.text
