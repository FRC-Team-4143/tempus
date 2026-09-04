"""Tests for `send_qr_dm` (app/services/slack_client.py): the QR badge DM now also
links to the unauthenticated `/badge/<id>` page as a save-the-image alternative."""
import app.services.slack_client as slack_client_mod
from app.config import settings
from app.services.badge import compute_badge_id


class _FakeSlackClient:
    def __init__(self):
        self.uploads = []

    async def conversations_open(self, users):
        return {"channel": {"id": f"D-{users}"}}

    async def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)
        return {"ok": True}


def _stub_client(monkeypatch):
    fake = _FakeSlackClient()
    monkeypatch.setattr(slack_client_mod, "get_slack_client", lambda: fake)
    return fake


async def test_send_qr_dm_includes_badge_page_link(monkeypatch):
    fake = _stub_client(monkeypatch)

    ok = await slack_client_mod.send_qr_dm("USTU", "ada00001", "Ada Lovelace")

    assert ok is True
    assert len(fake.uploads) == 1
    comment = fake.uploads[0]["initial_comment"]
    expected_url = f"{settings.base_url}/badge/{compute_badge_id('ada00001')}"
    assert expected_url in comment
    assert "doesn't require a Legion login" in comment
    assert "camera roll" in comment


async def test_send_qr_dm_omits_wallet_links_when_unconfigured(monkeypatch):
    fake = _stub_client(monkeypatch)
    # Don't depend on the dev's real .env — force the feature off.
    monkeypatch.setattr("app.services.wallet.apple_wallet_configured", lambda: False)
    monkeypatch.setattr("app.services.wallet.google_wallet_configured", lambda: False)

    await slack_client_mod.send_qr_dm("USTU", "ada00001", "Ada Lovelace")

    comment = fake.uploads[0]["initial_comment"]
    assert "Apple Wallet" not in comment
    assert "Google Wallet" not in comment


async def test_send_qr_dm_includes_wallet_links_when_configured(monkeypatch):
    fake = _stub_client(monkeypatch)
    monkeypatch.setattr("app.services.wallet.apple_wallet_configured", lambda: True)
    monkeypatch.setattr("app.services.wallet.google_wallet_configured", lambda: True)

    await slack_client_mod.send_qr_dm("USTU", "ada00001", "Ada Lovelace")

    comment = fake.uploads[0]["initial_comment"]
    badge_id = compute_badge_id("ada00001")
    assert f"<{settings.base_url}/wallet/apple/{badge_id}.pkpass|Add to Apple Wallet>" in comment
    assert f"<{settings.base_url}/wallet/google/{badge_id}|Add to Google Wallet>" in comment


async def test_send_qr_dm_returns_false_on_failure(monkeypatch):
    fake = _stub_client(monkeypatch)

    async def _boom(**kwargs):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(fake, "files_upload_v2", _boom)

    ok = await slack_client_mod.send_qr_dm("USTU", "ada00001", "Ada Lovelace")

    assert ok is False
