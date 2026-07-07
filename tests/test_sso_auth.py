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


async def test_read_sso_token_roundtrip():
    token = make_sso_cookie(groups=["tempus-admin"], name="Ada")
    claims = read_sso_token(token)
    assert claims is not None
    assert claims["name"] == "Ada"
    assert "tempus-admin" in claims["groups"]
    assert read_sso_token("garbage") is None
    assert read_sso_token(None) is None
