"""A magic-link identity must never reach /admin.

Legion mints link-borne cookies with `groups: []` and `via: "link"` (see its
`services/sso.make_link_sso_token`) precisely because a link is a bearer credential —
anyone who can read the Slack message holding it can redeem it. `/admin` therefore
bounces such an identity to a real sign-in rather than serving it, and rather than
403ing it, since the person may genuinely be an admin who just arrived from Slack.
"""
from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie


async def test_admin_redirects_a_magic_link_identity_to_sign_in(client):
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=(), via="link"))

    resp = await client.get("/admin", follow_redirects=False)

    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


async def test_admin_still_refuses_a_link_identity_that_somehow_carries_groups(client):
    """Belt and braces: `via` is checked before groups, so even a cookie minted with
    admin groups (which Legion will not do) can't ride a link into /admin."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=("tempus-admin",), via="link"))

    resp = await client.get("/admin", follow_redirects=False)

    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


async def test_a_normal_admin_cookie_still_gets_in(client):
    """Guard against the check above over-reaching into the ordinary sign-in path."""
    client.cookies.set(SSO_COOKIE, make_sso_cookie())

    resp = await client.get("/admin", follow_redirects=False)

    assert resp.status_code == 200
