"""IP-gating for the kiosk/mentor display routes (`_require_onsite` in
routers/kiosk.py) — reuses the same `signin_ip_whitelist` CIDRs that already gate
`POST /kiosk/signin`. The `client` fixture's ASGI transport always presents as
127.0.0.1 (httpx.ASGITransport's default synthetic client), so these tests toggle
`settings.signin_ip_whitelist` around that fixed address rather than needing a
second transport.

The two SSE routes (`/kiosk/stream`, `/mentor/stream`) only get the *blocked* path
exercised over HTTP here — on success their generator streams forever (by design,
for a live-updating kiosk board), which would hang a test client that waits for the
response to complete. Blocked requests never reach the generator at all (FastAPI
raises from the dependency before the endpoint body runs), so that path is safe to
hit directly; the allowed/blocked *logic* itself is covered for every route,
streaming or not, by the direct `_require_onsite` unit tests below."""
import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers.kiosk import _require_onsite

# Routes whose response completes normally and can be fetched with a plain GET.
_NON_STREAMING_ROUTES = [
    "/kiosk",
    "/kiosk/data",
    "/kiosk/stats",
    "/mentor",
    "/mentor/data",
    "/mentor/stats",
]
_STREAMING_ROUTES = ["/kiosk/stream", "/mentor/stream"]


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host):
        self.client = _FakeClient(host)


def test_require_onsite_allows_when_whitelist_blank(monkeypatch):
    monkeypatch.setattr(settings, "signin_ip_whitelist", "")
    _require_onsite(_FakeRequest("203.0.113.5"))  # does not raise


def test_require_onsite_blocks_ip_outside_whitelist(monkeypatch):
    monkeypatch.setattr(settings, "signin_ip_whitelist", "10.0.0.0/8")
    with pytest.raises(HTTPException) as exc_info:
        _require_onsite(_FakeRequest("203.0.113.5"))
    assert exc_info.value.status_code == 403


def test_require_onsite_allows_ip_inside_whitelist(monkeypatch):
    monkeypatch.setattr(settings, "signin_ip_whitelist", "10.0.0.0/8,192.168.0.0/16")
    _require_onsite(_FakeRequest("192.168.1.42"))  # does not raise


async def test_display_routes_open_by_default(client):
    """Blank whitelist (the out-of-the-box default) must not block anything — this is
    what keeps local dev/testing working with no configuration."""
    assert settings.signin_ip_whitelist == ""
    for path in _NON_STREAMING_ROUTES:
        resp = await client.get(path)
        assert resp.status_code == 200, path


async def test_display_routes_blocked_when_whitelist_excludes_client(client, monkeypatch):
    monkeypatch.setattr(settings, "signin_ip_whitelist", "10.0.0.0/8")
    for path in _NON_STREAMING_ROUTES + _STREAMING_ROUTES:
        resp = await client.get(path)
        assert resp.status_code == 403, path


async def test_display_routes_allowed_when_whitelist_includes_client(client, monkeypatch):
    monkeypatch.setattr(settings, "signin_ip_whitelist", "127.0.0.0/8")
    for path in _NON_STREAMING_ROUTES:
        resp = await client.get(path)
        assert resp.status_code == 200, path


async def test_kiosk_demo_never_gated(client, monkeypatch):
    """/kiosk/demo renders fake names only — it stays reachable from anywhere even
    when a restrictive whitelist would block the real display routes."""
    monkeypatch.setattr(settings, "signin_ip_whitelist", "10.0.0.0/8")
    resp = await client.get("/kiosk/demo")
    assert resp.status_code == 200


async def test_signin_post_unaffected_by_new_dependency(client, monkeypatch):
    """POST /kiosk/signin keeps its own in-band 'not allowed' JSON response (unchanged
    behavior) rather than the new dependency's 403 — it was never wired to
    _require_onsite, only _is_allowed_ip directly."""
    monkeypatch.setattr(settings, "signin_ip_whitelist", "10.0.0.0/8")
    resp = await client.post("/kiosk/signin", json={"name": "Nobody"})
    assert resp.status_code == 200
    assert resp.json()["success"] is False
