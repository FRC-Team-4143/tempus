from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    # extra="ignore": tolerate leftover keys in a deployed .env (e.g. the retired
    # ADMIN_PASSWORD/SESSION_SECRET) instead of failing to boot.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # Legion SSO — /admin is gated by the shared `mw_sso` cookie + the `tempus-admin`
    # group. Tempus only *verifies* the cookie (Legion mints it); `sso_secret` must equal
    # Legion's SSO_SECRET. There is no local admin password — the first admin is granted
    # the `tempus-admin` group in Legion's /admin/groups.
    sso_secret: str = ""
    sso_session_ttl: int = 43200  # 12h; must match Legion's cookie max_age
    sso_cookie_domain: str = ""   # e.g. ".marswars.org" so one login spans subdomains

    # Legion roster API — the read-only source of truth Tempus mirrors from.
    legion_base_url: str = ""     # e.g. "https://legion.marswars.org"
    legion_api_key: str = ""      # presented as X-API-Key to Legion's /api/* and /sso/challenge

    # Tempus's own public URL — used to build absolute links back to /me in Slack
    # messages (e.g. the /hours "open my dashboard" link). Prod: https://tempus.<domain>
    # (whose host must be in Legion's SSO_ALLOWED_RETURN_HOSTS, as /admin already needs).
    base_url: str = "http://localhost:8000"

    database_url: str = "sqlite+aiosqlite:///./tracker.db"

    auto_signout_time: str = "22:00"  # HH:MM 24h local time

    # "Wall of Shame" meme: when the nightly auto sign-out closes forgotten
    # sessions, post a lighthearted meme naming the kids who forgot to sign out.
    roast_enabled: bool = False  # global kill switch (opt-in)
    slack_announce_channel: str = ""  # Slack channel ID to post the meme into

    weekly_dm_day: int = 6   # 0=Mon ... 6=Sun
    weekly_dm_time: str = "21:00"  # HH:MM 24h local time

    # How long the combined kiosk display holds on the mentor board after a mentor
    # badge scan before returning to students. Any student scan cuts it short.
    kiosk_mentor_hold_seconds: int = 120

    timezone: str = "America/New_York"

    # Hours multipliers per session status (1.0 = full hours, 0.5 = 50%, 0.0 = none)
    # 'auto' always uses the same multiplier as 'contributor'
    contributor_multiplier: float = 1.0
    present_multiplier: float = 0.5
    distraction_multiplier: float = 0.0

    # Database backups (SQLite only)
    backup_dir: str = "backups"
    backup_keep: int = 14  # number of nightly snapshots to retain
    backup_time: str = "23:30"  # HH:MM 24h local time for the nightly snapshot

    # Global toggle for all automated updates (Slack messages, memes, scheduled jobs)
    updates_enabled: bool = True

    # ── Apple Wallet / Google Wallet passes (optional) ───────────────────────────
    # When configured, the /badge page and the /qr Slack DM offer "Add to Apple/Google
    # Wallet". A wallet pass carries the same kiosk QR but makes the phone max its screen
    # brightness automatically when shown, so the scanner reads it first try. Passes are
    # signed/built entirely in-process — no third-party pass service. Blank = disabled
    # (the /wallet/* routes 404 and no buttons render). Keep cert files outside the repo.
    #
    # Apple: needs a paid Apple Developer account -> a Pass Type ID + its certificate,
    # exported from Keychain as .p12, plus Apple's WWDR G4 intermediate (PEM or DER/.cer).
    apple_wallet_p12_path: str = ""
    apple_wallet_p12_password: str = ""
    apple_wallet_wwdr_path: str = ""
    apple_wallet_pass_type_id: str = ""   # e.g. "pass.org.marswars.tempus"
    apple_wallet_team_id: str = ""        # the 10-char Apple Developer Team ID
    apple_wallet_org_name: str = "Tempus"  # shown on the pass as the issuer
    #
    # Google: a Google Cloud project with the Google Wallet API enabled, a service-account
    # key (JSON), and your Wallet API issuer ID. The "Add to Google Wallet" link is a
    # locally-signed JWT — Tempus never calls the Wallet REST API.
    google_wallet_service_account_path: str = ""
    google_wallet_issuer_id: str = ""


settings = Settings()
