"""Archived mentors must not show up in the kiosk mentor leaderboard/crown — mirrors
the already-correct student-side filtering in kiosk.py's kiosk_stats."""
from datetime import datetime, timedelta

from app.models import Mentor, MentorSession
from app.routers.kiosk import _mentor_alltime_leader_id


async def _add_mentor(db, name, is_active=True):
    m = Mentor(name=name, slack_user_id=f"U{name}", is_active=is_active)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_mentor_session(db, mentor_id, hours):
    now = datetime.utcnow()
    db.add(MentorSession(
        mentor_id=mentor_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def test_archived_mentor_excluded_from_alltime_leader(db):
    archived = await _add_mentor(db, "Old Coach", is_active=False)
    await _add_mentor_session(db, archived.id, hours=100.0)  # would win if counted

    active = await _add_mentor(db, "Current Coach", is_active=True)
    await _add_mentor_session(db, active.id, hours=2.0)

    leader_id = await _mentor_alltime_leader_id(db)

    assert leader_id == active.id
