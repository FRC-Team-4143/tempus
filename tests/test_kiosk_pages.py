"""The kiosk and mentor board are public physical-display pages with no auth gate.
Their nav used to include a plain, unguarded "Admin" link — removed since discovery of
/admin now happens through Legion's app launcher instead (see services/home.py in the
Legion repo), and a dead-looking public button was more confusing than useful."""


async def test_kiosk_page_has_no_admin_link(client):
    resp = await client.get("/kiosk")
    assert resp.status_code == 200
    assert 'href="/admin"' not in resp.text


async def test_kiosk_page_has_favicon(client):
    resp = await client.get("/kiosk")
    assert resp.status_code == 200
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in resp.text


async def test_mentor_page_has_no_admin_link(client):
    resp = await client.get("/mentor")
    assert resp.status_code == 200
    assert 'href="/admin"' not in resp.text
