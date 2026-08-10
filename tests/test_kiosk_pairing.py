"""Device pairing for the kiosk boards (`services/kiosk_device.py`), which replaced
the old `signin_ip_whitelist` CIDR gate.

Gating is asserted over *both* boards and all their children, since "the mentor board
is locked behind the same pairing" is the whole point of the /kiosk/* restructure.

The two SSE routes only get the *blocked* path: the dependency raises before the
generator runs, so a 403 returns normally, whereas a successful stream never completes
and would hang the client.
"""
import pytest
from sqlalchemy import func, select

from app.models import KioskDevice, KioskDeviceStatus
from app.services import kiosk_device
from app.services.kiosk_device import KIOSK_COOKIE
from tests.conftest import make_sso_cookie, pair_device

_PAGES = ["/kiosk", "/kiosk/student", "/kiosk/mentor"]
_JSON_ROUTES = [
    "/kiosk/student/data", "/kiosk/student/stats",
    "/kiosk/mentor/data", "/kiosk/mentor/stats",
]
_STREAM_ROUTES = ["/kiosk/student/stream", "/kiosk/mentor/stream"]


async def _pending_rows(db):
    return (await db.execute(select(func.count()).select_from(KioskDevice))).scalar_one()


async def _open_window(db):
    await kiosk_device.open_pairing(db)


# ── Unpaired ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", _PAGES)
async def test_board_pages_show_pairing_screen_when_unpaired(client, db, path):
    await _open_window(db)
    resp = await client.get(path)
    assert resp.status_code == 200
    assert "PAIR" in resp.text.upper() or "isn" in resp.text
    assert "MARS-" in resp.text          # a code was issued
    assert KIOSK_COOKIE in resp.cookies  # provisional cookie set on the same response


@pytest.mark.parametrize("path", _JSON_ROUTES + _STREAM_ROUTES)
async def test_json_and_sse_routes_403_when_unpaired(client, path):
    resp = await client.get(path)
    assert resp.status_code == 403, path


async def test_signin_returns_soft_200_when_unpaired(client):
    """Not a 403: the kiosk renders `message` in its toast, so an unpaired display
    tells the student what's wrong instead of failing silently."""
    resp = await client.post("/kiosk/signin", json={"name": "whoever"})
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "not paired" in resp.json()["message"].lower()


async def test_repeat_load_reuses_the_pending_row(client, db):
    """A display that reloads — or whose poll races an approval — must not pile up
    rows. The cookie-first branch of the gate is what guarantees that."""
    await _open_window(db)
    await client.get("/kiosk")
    assert await _pending_rows(db) == 1

    for _ in range(5):
        resp = await client.get("/kiosk")
        assert resp.status_code == 200
    assert await _pending_rows(db) == 1


async def test_tampered_cookie_is_treated_as_unpaired(client, db):
    await pair_device(db, client)
    client.cookies.set(KIOSK_COOKIE, "not-a-valid-signed-value")
    resp = await client.get("/kiosk/student/data")
    assert resp.status_code == 403


async def test_demo_is_never_gated(client):
    """Hardcoded fake names only, so it stays reachable from anywhere — including
    with pairing closed, which is the normal state."""
    resp = await client.get("/kiosk/demo")
    assert resp.status_code == 200


# ── Paired ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", _PAGES)
async def test_board_pages_render_when_paired(paired_client, path):
    resp = await paired_client.get(path)
    assert resp.status_code == 200
    assert "MARS-" not in resp.text  # the real board, not the pairing screen


@pytest.mark.parametrize("path", _JSON_ROUTES)
async def test_json_routes_serve_when_paired(paired_client, path):
    resp = await paired_client.get(path)
    assert resp.status_code == 200


async def test_stream_routes_disable_proxy_buffering():
    """The app sits behind an nginx reverse proxy (see README), which buffers
    proxied responses by default — silently delaying every SSE push (including
    mentor_update, which every open board relies on to keep its counts current)
    until the buffer fills or the connection closes. Without
    `X-Accel-Buffering: no`, a kiosk display can look like it's simply not
    reacting to badge scans elsewhere.

    Called directly rather than through a client: the generator body never runs
    until the ASGI server starts pulling from it, so this only inspects the
    StreamingResponse's headers without opening the (infinite) stream — which,
    per this module's own docstring, would hang an in-process test client.
    """
    from app.routers.kiosk import kiosk_stream, mentor_stream

    for make_response in (kiosk_stream, mentor_stream):
        resp = await make_response()
        assert resp.headers["x-accel-buffering"] == "no"
        assert resp.headers["cache-control"] == "no-cache"


async def test_one_pairing_grants_both_boards(paired_client):
    """A display is paired once, not once per board."""
    assert (await paired_client.get("/kiosk/student")).status_code == 200
    assert (await paired_client.get("/kiosk/mentor")).status_code == 200
    assert (await paired_client.get("/kiosk/student/data")).status_code == 200
    assert (await paired_client.get("/kiosk/mentor/data")).status_code == 200


async def test_revoke_drops_the_display_back_to_pairing(paired_client, db):
    device = (await db.execute(select(KioskDevice))).scalars().first()
    assert (await paired_client.get("/kiosk/student/data")).status_code == 200

    device.status = KioskDeviceStatus.revoked
    await db.commit()

    # The cookie is signed but never authoritative, so this takes effect at once.
    assert (await paired_client.get("/kiosk/student/data")).status_code == 403
    resp = await paired_client.get("/kiosk")
    assert resp.status_code == 200
    assert "isn" in resp.text  # "This display isn't paired."


# ── The poll endpoint ───────────────────────────────────────────────────────

async def test_pair_status_reports_unknown_without_a_cookie(client):
    resp = await client.get("/kiosk/pair/status")
    assert resp.json() == {"status": "unknown"}


async def test_pair_status_transitions_pending_to_active(client, db):
    await _open_window(db)
    await client.get("/kiosk")
    assert (await client.get("/kiosk/pair/status")).json() == {"status": "pending"}

    device = (await db.execute(select(KioskDevice))).scalars().first()
    device.status = KioskDeviceStatus.active
    await db.commit()

    assert (await client.get("/kiosk/pair/status")).json() == {"status": "active"}


# ── Legacy alias ────────────────────────────────────────────────────────────

async def test_mentor_alias_redirects(client):
    resp = await client.get("/mentor", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/kiosk/mentor"


async def test_mentor_alias_redirects_even_when_unpaired(client):
    """The alias must not depend on the gate — the pairing check belongs at the
    destination, so this stays a trivial redirect that can't drift."""
    resp = await client.get("/mentor", follow_redirects=False)
    assert resp.status_code == 302


# ── Admin surface ───────────────────────────────────────────────────────────

async def test_admin_can_approve_a_pending_device(client, db):
    await _open_window(db)
    await client.get("/kiosk")
    device = (await db.execute(select(KioskDevice))).scalars().first()
    assert device.status == KioskDeviceStatus.pending

    from app.services.sso import SSO_COOKIE
    client.cookies.set(SSO_COOKIE, make_sso_cookie())
    resp = await client.post(
        f"/admin/kiosk-devices/{device.id}/approve",
        data={"label": "Shop kiosk"}, follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(device)
    assert device.status == KioskDeviceStatus.active
    assert device.label == "Shop kiosk"
    assert device.approved_by == "Test Admin"


async def test_admin_deny_deletes_the_row(authed_client, db, client):
    await _open_window(db)
    await client.get("/kiosk")
    device = (await db.execute(select(KioskDevice))).scalars().first()

    resp = await authed_client.post(
        f"/admin/kiosk-devices/{device.id}/deny", follow_redirects=False
    )
    assert resp.status_code == 303
    assert await _pending_rows(db) == 0


async def test_manager_is_denied_the_kiosk_devices_page(client):
    """Admin-only by omission — the path isn't in `_manager_allowed`."""
    from app.services.sso import SSO_COOKIE

    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get("/admin/kiosk-devices")
    assert resp.status_code == 403
    assert "No Access" in resp.text


async def test_pairing_actions_are_audited(authed_client, db):
    from app.models import AuditLog

    await authed_client.post(
        "/admin/kiosk-devices/pair-window", data={"action": "open"},
        follow_redirects=False,
    )
    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "kiosk_device.pairing_open" in actions
