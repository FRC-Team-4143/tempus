"""
Admin SSO gate — /admin is protected by Legion's `mw_sso` cookie + the `tempus-admin`
group. There is no local password login anymore.
"""
from datetime import datetime, timedelta

import pytest

from app.models import AttendanceSession
from app.services.sso import SSO_COOKIE, read_sso_token
from tests.conftest import make_sso_cookie

pytestmark = pytest.mark.asyncio


async def test_no_cookie_redirects_to_legion(client):
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/authorize?app=tempus" in resp.headers["location"]


async def test_valid_admin_cookie_allowed(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"]))
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 200


async def test_signed_in_without_group_forbidden(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["munus-admin"]))
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 403


async def test_tampered_cookie_redirects(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie() + "tampered")
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]


async def test_logout_bounces_to_legion(client):
    resp = await client.get("/admin/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/logout" in resp.headers["location"]


# ── tempus-manager tier: dashboard + report view + requirements editor only ────

async def test_manager_can_reach_dashboard(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 200


async def test_manager_can_reach_report(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/report", follow_redirects=False)
    assert resp.status_code == 200


async def test_manager_can_reach_report_export(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/report/export", follow_redirects=False)
    assert resp.status_code == 200


async def test_manager_denied_page_stays_in_admin_shell(client):
    """Regression test: a manager hitting a disallowed page used to be silently
    303-redirected to the dashboard — more disorienting than informative ("why did
    clicking Settings send me to the dashboard?"). It now stays on the same admin
    shell (sidebar still fully visible/clickable) with a blurred placeholder + "No
    Access" badge naming the section."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/roster", follow_redirects=False)
    assert resp.status_code == 403
    assert 'href="/admin/report"' in resp.text  # sidebar still rendered
    assert "No Access" in resp.text
    assert "Roster" in resp.text


async def test_manager_is_denied_from_settings(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/settings", follow_redirects=False)
    assert resp.status_code == 403
    assert "Settings" in resp.text


async def test_manager_can_reach_requirements(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/requirements", follow_redirects=False)
    assert resp.status_code == 200


async def test_manager_can_create_requirement(client, db):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.post(
        "/admin/requirements",
        data={"week_start": "2026-08-03", "required_hours": "6"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/requirements"

    from sqlalchemy import select
    from app.models import WeeklyRequirement
    result = await db.execute(select(WeeklyRequirement))
    reqs = result.scalars().all()
    assert len(reqs) == 1
    assert reqs[0].required_hours == 6


async def test_manager_can_delete_requirement(client, db):
    from datetime import date
    from app.models import WeeklyRequirement
    req = WeeklyRequirement(team_id=None, subteam_slug=None, week_start=date(2026, 8, 3), required_hours=6)
    db.add(req)
    await db.commit()
    await db.refresh(req)

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.post(f"/admin/requirements/{req.id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    from sqlalchemy import select
    result = await db.execute(select(WeeklyRequirement))
    assert result.scalars().all() == []


async def test_manager_can_reach_sessions_list(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/sessions", follow_redirects=False)
    assert resp.status_code == 200


async def test_manager_can_edit_session(client, db, make_student):
    student = await make_student()
    now = datetime.utcnow()
    session = AttendanceSession(
        student_id=student.id, sign_in_time=now - timedelta(hours=2), sign_out_time=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.post(
        f"/admin/sessions/{session.id}/edit",
        data={
            "sign_in_time": (now - timedelta(hours=1)).isoformat(timespec="minutes"),
            "sign_out_time": now.isoformat(timespec="minutes"),
            "status": "contributor",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(session)
    assert session.status.value == "contributor"


async def test_manager_can_force_signout_session(client, db, make_student):
    student = await make_student()
    session = AttendanceSession(student_id=student.id, sign_in_time=datetime.utcnow())
    db.add(session)
    await db.commit()
    await db.refresh(session)

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.post(f"/admin/sessions/{session.id}/force-signout", follow_redirects=False)
    assert resp.status_code == 303

    await db.refresh(session)
    assert session.sign_out_time is not None
    assert session.auto_closed is True  # the manager's force-signout button, not a self-checkout


async def test_manager_can_delete_session(client, db, make_student):
    student = await make_student()
    session = AttendanceSession(
        student_id=student.id, sign_in_time=datetime.utcnow() - timedelta(hours=1),
        sign_out_time=datetime.utcnow(),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.post(f"/admin/sessions/{session.id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    from sqlalchemy import select
    result = await db.execute(select(AttendanceSession).where(AttendanceSession.id == session.id))
    assert result.scalar_one_or_none() is None


async def test_manager_dashboard_hides_manual_signin(client, db, make_student):
    """A manager can view the dashboard but can't act on it — the manual sign-in
    control should be hidden rather than shown-but-blocked."""
    await make_student()
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin")
    assert "Manual Sign-In" not in resp.text


async def test_admin_dashboard_still_shows_manual_signin(client, db, make_student):
    await make_student()
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"]))
    resp = await client.get("/admin")
    assert "Manual Sign-In" in resp.text


async def test_manager_sidebar_shows_all_links(client):
    """The sidebar shows every section to every tier, regardless of access — a
    manager clicking a disallowed one gets the blur-blocked "No Access" page rather
    than the tab being hidden outright."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin")
    assert 'href="/admin/roster"' in resp.text
    assert 'href="/admin/settings"' in resp.text
    assert 'href="/admin/report"' in resp.text


async def test_admin_sidebar_shows_all_links(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"]))
    resp = await client.get("/admin")
    assert 'href="/admin/roster"' in resp.text
    assert 'href="/admin/settings"' in resp.text


async def test_admin_sidebar_shows_legion_link_when_configured(client):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = "https://legion.example.org"
        client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"]))
        resp = await client.get("/admin")
        assert 'href="https://legion.example.org"' in resp.text
    finally:
        settings.legion_base_url = original


async def test_admin_sidebar_hides_legion_link_when_unconfigured(client):
    from app.config import settings
    original = settings.legion_base_url
    try:
        settings.legion_base_url = ""
        client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-admin"]))
        resp = await client.get("/admin")
        assert ">Legion</a>" not in resp.text
    finally:
        settings.legion_base_url = original


async def test_read_sso_token_roundtrip():
    token = make_sso_cookie(groups=["tempus-admin"], name="Ada")
    claims = read_sso_token(token)
    assert claims is not None
    assert claims["name"] == "Ada"
    assert "tempus-admin" in claims["groups"]
    assert read_sso_token("garbage") is None
    assert read_sso_token(None) is None
