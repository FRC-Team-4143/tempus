"""The kiosk boards are physical-display pages — no login/account is required, but a
browser must be a *paired device* to see one (see test_kiosk_pairing.py); an unpaired
one gets the pairing screen instead. Their nav used to include a plain, unguarded
"Admin" link — removed since discovery of /admin now happens through Legion's app
launcher instead (see services/home.py in the Legion repo), and a dead-looking public
button was more confusing than useful."""


async def test_kiosk_page_has_no_admin_link(paired_client):
    resp = await paired_client.get("/kiosk")
    assert resp.status_code == 200
    assert 'href="/admin"' not in resp.text


async def test_kiosk_page_has_favicon(paired_client):
    resp = await paired_client.get("/kiosk")
    assert resp.status_code == 200
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in resp.text


async def test_student_board_has_no_admin_link(paired_client):
    resp = await paired_client.get("/kiosk/student")
    assert resp.status_code == 200
    assert 'href="/admin"' not in resp.text


async def test_mentor_page_has_no_admin_link(paired_client):
    resp = await paired_client.get("/kiosk/mentor")
    assert resp.status_code == 200
    assert 'href="/admin"' not in resp.text
