"""Tests for the editable admin Settings page.

Covers the generalized `.env` writeback + live-singleton update for the
non-secret config fields, and validation (bad timezone is rejected).
The scheduler is not started under the test transport, so the handler's
``reschedule_all`` call is a safe no-op (``app.state.scheduler`` is absent).
"""
import pytest

from app.config import settings
from app.routers import admin

# Fields the settings POST may mutate on the global singleton; snapshot/restore
# so a test never leaks config into the others.
_MUTABLE = [
    "contributor_multiplier", "present_multiplier", "distraction_multiplier",
    "auto_signout_time", "weekly_dm_day", "weekly_dm_time",
    "backup_time", "backup_keep", "timezone", "signin_ip_whitelist",
    "slack_announce_channel", "updates_enabled", "roast_enabled",
]


@pytest.fixture
def restore_settings():
    snapshot = {k: getattr(settings, k) for k in _MUTABLE}
    yield
    for k, v in snapshot.items():
        setattr(settings, k, v)


def _form(**overrides):
    """A complete settings form pre-filled from the current singleton."""
    form = {
        "contributor_multiplier": settings.contributor_multiplier,
        "present_multiplier": settings.present_multiplier,
        "distraction_multiplier": settings.distraction_multiplier,
        "auto_signout_time": settings.auto_signout_time,
        "auto_signout_effective": "",
        "weekly_dm_day": settings.weekly_dm_day,
        "weekly_dm_time": settings.weekly_dm_time,
        "backup_time": settings.backup_time,
        "backup_keep": settings.backup_keep,
        "timezone": settings.timezone,
        "signin_ip_whitelist": settings.signin_ip_whitelist,
        "slack_announce_channel": settings.slack_announce_channel,
        "leaderboard_since": "",
    }
    form.update(overrides)
    return form


async def test_settings_post_writes_env_and_updates_singleton(
    authed_client, tmp_path, monkeypatch, restore_settings
):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))

    # Known baseline so every POSTed value below is an actual change (the handler
    # only writes fields that differ from the live singleton). restore_settings
    # puts the originals back afterward.
    settings.weekly_dm_time = "21:00"
    settings.backup_keep = 14
    settings.timezone = "America/New_York"
    settings.slack_announce_channel = ""
    settings.roast_enabled = False

    resp = await authed_client.post("/admin/settings", data=_form(
        weekly_dm_time="20:15",
        backup_keep="30",
        timezone="America/Denver",
        slack_announce_channel="C0DEADBEEF",
        roast_enabled="true",
        updates_enabled="true",
    ))

    assert resp.status_code == 200
    assert "Settings saved" in resp.text

    # Live singleton updated immediately.
    assert settings.weekly_dm_time == "20:15"
    assert settings.backup_keep == 30
    assert settings.timezone == "America/Denver"
    assert settings.slack_announce_channel == "C0DEADBEEF"
    assert settings.roast_enabled is True

    # Persisted to .env for the next restart.
    written = env_file.read_text()
    assert "WEEKLY_DM_TIME=20:15" in written
    assert "BACKUP_KEEP=30" in written
    assert "TIMEZONE=America/Denver" in written
    assert "SLACK_ANNOUNCE_CHANNEL=C0DEADBEEF" in written
    assert "ROAST_ENABLED=true" in written


async def test_settings_post_cannot_inject_env_lines(
    authed_client, tmp_path, monkeypatch, restore_settings
):
    """Regression test: an embedded newline in a free-text setting must not let an
    admin inject an arbitrary extra KEY=VALUE line into .env (e.g. SSO_SECRET)."""
    env_file = tmp_path / ".env"
    env_file.write_text("SSO_SECRET=original-secret\n")
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    settings.slack_announce_channel = ""

    resp = await authed_client.post("/admin/settings", data=_form(
        slack_announce_channel="C0DEADBEEF\nSSO_SECRET=pwned",
    ))
    assert resp.status_code == 200

    written_lines = env_file.read_text().splitlines()
    # SSO_SECRET must still be exactly one, untouched line — the injected value collapses
    # onto a single SLACK_ANNOUNCE_CHANNEL line instead of creating a second SSO_SECRET=.
    assert [l for l in written_lines if l.startswith("SSO_SECRET=")] == ["SSO_SECRET=original-secret"]
    assert "SLACK_ANNOUNCE_CHANNEL=C0DEADBEEFSSO_SECRET=pwned" in written_lines


async def test_settings_post_rejects_bad_timezone(
    authed_client, tmp_path, monkeypatch, restore_settings
):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", str(env_file))
    original_tz = settings.timezone

    resp = await authed_client.post("/admin/settings", data=_form(
        timezone="Not/AZone",
    ))

    assert resp.status_code == 200
    assert "Unknown timezone" in resp.text
    # Singleton unchanged and nothing bad written to .env.
    assert settings.timezone == original_tz
    if env_file.exists():
        assert "Not/AZone" not in env_file.read_text()
