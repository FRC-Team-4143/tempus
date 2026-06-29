from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    admin_password: str = "changeme"
    session_secret: str = "dev-secret-change-in-production"

    database_url: str = "sqlite+aiosqlite:///./tracker.db"

    auto_signout_time: str = "22:00"  # HH:MM 24h local time

    weekly_dm_day: int = 6   # 0=Mon ... 6=Sun
    weekly_dm_time: str = "21:00"  # HH:MM 24h local time

    signin_ip_whitelist: str = ""  # comma-separated CIDRs, blank = no restriction

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


settings = Settings()
