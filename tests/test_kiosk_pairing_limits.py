"""Abuse resistance for kiosk pairing.

`/kiosk` is unauthenticated by necessity — an unpaired display has to be able to
render *something* — so anyone who can reach BASE_URL can hit the enrollment path.
Five layers keep that from turning into either unbounded row growth or a flooded
admin approve page. Each is tested independently here, because each alone is
insufficient and a regression in any one of them is invisible from the others.

Note a bare row cap would be a *bug*, not a fix: fill it and a legitimate display
can no longer enroll. The real protection is that pairing is closed by default.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import KioskDevice, KioskDeviceStatus
from app.services import kiosk_device
from app.services.app_settings import set_setting
from app.services.kiosk_device import KIOSK_COOKIE, KIOSK_PAIRING_OPEN_UNTIL_KEY


async def _rows(db):
    return (await db.execute(select(func.count()).select_from(KioskDevice))).scalar_one()


def _forget_device(client):
    """Drop the kiosk cookie so the next load looks like a brand-new browser."""
    client.cookies.delete(KIOSK_COOKIE)


# ── Layer 1: the window is closed by default ────────────────────────────────

async def test_closed_window_creates_no_rows(client, db):
    """The structural fix. Assert on the row count, not just the markup — the
    point is that nothing is written at all, not merely that a code is hidden."""
    assert await _rows(db) == 0

    for _ in range(10):
        resp = await client.get("/kiosk")
        assert resp.status_code == 200
        assert "MARS-" not in resp.text
        _forget_device(client)

    assert await _rows(db) == 0


async def test_closed_window_sets_no_cookie(client):
    resp = await client.get("/kiosk")
    assert KIOSK_COOKIE not in resp.cookies


async def test_expired_window_behaves_like_never_opened(client, db):
    """A stored time in the past must read as closed, so an admin who forgets to
    close the window isn't leaving enrollment open indefinitely."""
    past = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    await set_setting(db, KIOSK_PAIRING_OPEN_UNTIL_KEY, past)

    assert await kiosk_device.pairing_open_until(db) is None
    resp = await client.get("/kiosk")
    assert "MARS-" not in resp.text
    assert await _rows(db) == 0


async def test_open_window_allows_exactly_one_row(client, db):
    await kiosk_device.open_pairing(db)
    resp = await client.get("/kiosk")
    assert "MARS-" in resp.text
    assert await _rows(db) == 1


# ── Layer 2: cookie-first reuse ─────────────────────────────────────────────

async def test_same_browser_reuses_its_row_and_code(client, db):
    await kiosk_device.open_pairing(db)
    first = await client.get("/kiosk")
    code = (await db.execute(select(KioskDevice.user_code))).scalars().one()
    assert code in first.text

    for _ in range(8):
        resp = await client.get("/kiosk")
        assert code in resp.text        # same code, not a fresh one
    assert await _rows(db) == 1


# ── Layer 3: per-IP token bucket ───────────────────────────────────────────

async def test_per_ip_bucket_caps_row_creation(client, db):
    """Distinct browsers from one address (the ASGI transport always presents as
    127.0.0.1) stop creating rows once the bucket is spent."""
    await kiosk_device.open_pairing(db)

    for _ in range(15):
        await client.get("/kiosk")
        _forget_device(client)

    assert await _rows(db) == kiosk_device._RATE_LIMIT_MAX


async def test_throttled_browser_gets_the_inert_page(client, db):
    await kiosk_device.open_pairing(db)
    for _ in range(kiosk_device._RATE_LIMIT_MAX):
        await client.get("/kiosk")
        _forget_device(client)

    resp = await client.get("/kiosk")
    assert resp.status_code == 200
    assert "MARS-" not in resp.text
    assert "try again" in resp.text.lower()


# ── Layer 4: pending cap ───────────────────────────────────────────────────

async def test_pending_cap_blocks_further_rows(client, db, monkeypatch):
    """Guards against a distributed flood, where the per-IP bucket doesn't bite.
    Rows are inserted directly so this tests the cap rather than the throttle."""
    await kiosk_device.open_pairing(db)
    for i in range(kiosk_device.MAX_PENDING):
        db.add(KioskDevice(
            device_id=f"flood{i:04d}", user_code=f"MARS-F{i:03d}",
            status=KioskDeviceStatus.pending, created_at=datetime.utcnow(),
        ))
    await db.commit()

    resp = await client.get("/kiosk")
    assert "MARS-F" not in resp.text          # not handed someone else's code
    assert "try again" in resp.text.lower()
    assert await _rows(db) == kiosk_device.MAX_PENDING


# ── Layer 5: pruning ───────────────────────────────────────────────────────

async def test_prune_drops_stale_pending_and_spares_fresh(db):
    stale_at = datetime.utcnow() - timedelta(minutes=kiosk_device.PAIRING_WINDOW_MINUTES + 5)
    db.add(KioskDevice(device_id="stale001", user_code="MARS-OLD",
                       status=KioskDeviceStatus.pending, created_at=stale_at))
    db.add(KioskDevice(device_id="fresh001", user_code="MARS-NEW",
                       status=KioskDeviceStatus.pending, created_at=datetime.utcnow()))
    await db.commit()

    await kiosk_device.prune_pending(db)
    await db.commit()

    remaining = (await db.execute(select(KioskDevice.user_code))).scalars().all()
    assert remaining == ["MARS-NEW"]


async def test_prune_never_touches_paired_devices(db):
    """An active display can be months old — pruning must key on status, not age."""
    old = datetime.utcnow() - timedelta(days=400)
    db.add(KioskDevice(device_id="old00001", user_code="MARS-AGED",
                       status=KioskDeviceStatus.active, created_at=old,
                       approved_at=old))
    await db.commit()

    await kiosk_device.prune_pending(db)
    await db.commit()

    assert await _rows(db) == 1


# ── The poll endpoint is not an amplification vector ───────────────────────

async def test_polling_status_never_creates_rows(client, db):
    await kiosk_device.open_pairing(db)
    for _ in range(20):
        resp = await client.get("/kiosk/pair/status")
        assert resp.status_code == 200
    assert await _rows(db) == 0


async def test_polling_status_does_not_consume_the_rate_limit(client, db):
    await kiosk_device.open_pairing(db)
    for _ in range(20):
        await client.get("/kiosk/pair/status")

    # The bucket is untouched, so a genuine display can still enroll.
    resp = await client.get("/kiosk")
    assert "MARS-" in resp.text
    assert await _rows(db) == 1
