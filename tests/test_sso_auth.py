"""
Admin SSO gate — /admin is protected by Legion's `mw_sso` cookie + the `tempus-admin`
group. There is no local password login anymore.
"""
import pytest

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


# ── tempus-manager tier: dashboard + report view only ──────────────────────────

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
