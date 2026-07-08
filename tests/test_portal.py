"""Tests for the personal portal at /me: open to any active student or mentor
(matched by Legion's member_code), independent of any /admin group."""
from datetime import date, datetime, timedelta

from app.models import AttendanceSession, Mentor, MentorSession
from app.services.app_settings import set_leaderboard_since
from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie


async def _add_mentor(db, *, code="mnt00001", name="Coach Ray", is_active=True):
    m = Mentor(name=name, member_code=code, slack_user_id=f"U{code}", is_active=is_active)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_session(db, student_id, hours, days_ago=0):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def _add_mentor_session(db, mentor_id, hours, days_ago=0):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(MentorSession(
        mentor_id=mentor_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def test_signed_out_shows_identify_with_signin_link(client):
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "Sign in with Legion" in resp.text


async def test_signed_in_no_matching_record_shows_not_synced(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ghost001"))
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "don't have an active student or mentor record" in resp.text


async def test_inactive_student_shows_not_synced(client, db, make_student):
    await make_student(code="grad0001", is_active=False)
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="grad0001"))
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "don't have an active student or mentor record" in resp.text


async def test_active_student_sees_own_data(client, db, make_student):
    student = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_session(db, student.id, hours=2.5)

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))
    resp = await client.get("/me")

    assert resp.status_code == 200
    assert "Hi, Ada" in resp.text
    assert "2.5" in resp.text


async def test_active_mentor_sees_own_data(client, db):
    mentor = await _add_mentor(db, code="mnt00001", name="Coach Ray")
    await _add_mentor_session(db, mentor.id, hours=3.0)

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="mnt00001", role="mentor"))
    resp = await client.get("/me")

    assert resp.status_code == 200
    assert "Hi, Coach" in resp.text
    assert "3.0" in resp.text


async def test_student_and_mentor_records_are_independent(client, db, make_student):
    """A student's member_code shouldn't accidentally match a mentor lookup or vice
    versa — the student check happens first and returns before the mentor query runs."""
    await make_student(name="Ada Lovelace", code="shared01")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="shared01", role="student"))
    resp = await client.get("/me")
    assert "Hi, Ada" in resp.text


async def test_total_hours_honors_leaderboard_since(client, db, make_student):
    student = await make_student(code="ada00001")
    await _add_session(db, student.id, hours=5.0, days_ago=30)  # before cutoff
    await _add_session(db, student.id, hours=2.0, days_ago=1)   # after cutoff

    await set_leaderboard_since(db, date.today() - timedelta(days=7))

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))
    resp = await client.get("/me")

    assert resp.status_code == 200
    assert "2.0" in resp.text
    assert "Counting since" in resp.text


async def test_admin_link_shown_for_tempus_admin(client, db, make_student):
    await make_student(code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"], member_code="ada00001", role="student"))
    resp = await client.get("/me")
    assert 'href="/admin"' in resp.text


async def test_admin_link_shown_for_tempus_manager(client, db, make_student):
    await make_student(code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"], member_code="ada00001", role="student"))
    resp = await client.get("/me")
    assert 'href="/admin"' in resp.text


async def test_admin_link_hidden_for_plain_member(client, db, make_student):
    await make_student(code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))
    resp = await client.get("/me")
    assert 'href="/admin"' not in resp.text


async def test_portal_navbar_shows_legion_link_when_configured(client, db, make_student):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = "https://legion.example.org"
        await make_student(code="ada00001")
        client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))
        resp = await client.get("/me")
        assert 'href="https://legion.example.org"' in resp.text
    finally:
        settings.legion_base_url = original


async def test_portal_navbar_hides_legion_link_when_unconfigured(client, db, make_student):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = ""
        await make_student(code="ada00001")
        client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))
        resp = await client.get("/me")
        assert ">Legion</a>" not in resp.text
    finally:
        settings.legion_base_url = original


async def test_dashboard_shows_admin_card_for_staff(client, db, make_student):
    await make_student(code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"], member_code="ada00001", role="student"))
    resp = await client.get("/me")
    assert "Open admin area" in resp.text


async def test_dashboard_hides_admin_card_for_plain_member(client, db, make_student):
    await make_student(code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))
    resp = await client.get("/me")
    assert "Open admin area" not in resp.text


async def test_portal_logout_redirects_to_legion_returning_to_me(client):
    resp = await client.get("/me/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/logout" in resp.headers["location"]
    assert "return_to" in resp.headers["location"]
