"""Tests for admin-initiated sign-out of a single open mentor session."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Mentor, MentorSession
from app.services.attendance import mentor_sign_out


async def _open_mentor_session(db, hours_ago: float = 2.0) -> MentorSession:
    mentor = Mentor(name="Grace Hopper", slack_user_id="U_MENTOR_1", is_active=True)
    db.add(mentor)
    await db.commit()
    await db.refresh(mentor)

    sess = MentorSession(
        mentor_id=mentor.id,
        sign_in_time=datetime.utcnow() - timedelta(hours=hours_ago),
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return sess


async def test_mentor_sign_out_closes_and_counts_full_hours(db):
    sess = await _open_mentor_session(db, hours_ago=2.0)

    result = await mentor_sign_out(db, sess.id)

    assert result is not None
    assert result.sign_out_time is not None
    # Mentors get full elapsed hours (no status multiplier).
    assert result.hours_counted == pytest.approx(2.0, abs=0.05)


async def test_mentor_sign_out_ignores_already_closed(db):
    sess = await _open_mentor_session(db)
    await mentor_sign_out(db, sess.id)

    # A second attempt finds no open session.
    assert await mentor_sign_out(db, sess.id) is None


async def test_mentor_sessions_page_shows_signout_button_without_meme(db, authed_client):
    sess = await _open_mentor_session(db, hours_ago=1.0)

    resp = await authed_client.get("/admin/sessions?person_type=mentor")
    assert resp.status_code == 200
    body = resp.text

    # Sign Out button + modal wired to the mentor force-signout endpoint.
    assert f"mentor-signout-modal-{sess.id}" in body
    assert f"/admin/mentor-sessions/{sess.id}/force-signout" in body
    # No "Wall of Shame" meme prompt on mentor sign-out.
    assert "Wall of Shame" not in body


async def test_force_signout_route_closes_open_mentor_session(db, authed_client):
    sess = await _open_mentor_session(db, hours_ago=1.0)

    resp = await authed_client.post(
        f"/admin/mentor-sessions/{sess.id}/force-signout",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(sess)
    assert sess.sign_out_time is not None
    assert sess.hours_counted == pytest.approx(1.0, abs=0.05)
