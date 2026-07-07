from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables and seed initial data."""
    from app.models import Team  # noqa: F401 – imported for side-effect (table creation)

    # Apply a staged database restore (if any) before the engine touches the file.
    from app.services.backup import apply_pending_restore
    apply_pending_restore()

    async with engine.begin() as conn:
        from app.database import Base
        await conn.run_sync(Base.metadata.create_all)
        # Safe migration: add checkout_requested_at if it doesn't exist yet
        await conn.run_sync(_add_checkout_requested_at_column)
        # Safe migration: allow NULL team_id (all-teams requirements)
        await conn.run_sync(_make_weekly_req_team_nullable)
        # Safe migration: add archive columns to students and mentors
        await conn.run_sync(_add_archive_columns)
        # Safe migration: drop abandoned Slack message tracking columns
        await conn.run_sync(_drop_slack_session_columns)
        # ── Legion integration migrations ──
        # Add the Legion `member_code` link/badge id to students and mentors
        await conn.run_sync(_add_member_code_columns)
        # Add the synced Legion group slugs to mentors (drives lead status)
        await conn.run_sync(_add_group_slugs_column)
        # Rename `category` (FocusCategory enum) → `subteam_slug` (Legion slug string)
        await conn.run_sync(_rename_category_to_subteam)
        await conn.run_sync(_weekly_req_category_to_subteam)
        # Retire the local `is_lead` flag (lead status now comes from Legion groups)
        await conn.run_sync(_drop_is_lead_column)

    await _seed_teams()
    await _seed_subteams()


def _add_member_code_columns(conn) -> None:
    """Add the nullable Legion `member_code` link to students and mentors."""
    from sqlalchemy import inspect, text
    for table in ("students", "mentors"):
        columns = [c["name"] for c in inspect(conn).get_columns(table)]
        if "member_code" not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN member_code VARCHAR(16)"))
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_member_code "
                f"ON {table} (member_code)"
            ))


def _add_group_slugs_column(conn) -> None:
    """Add the synced Legion group-slug JSON to mentors."""
    from sqlalchemy import inspect, text
    columns = [c["name"] for c in inspect(conn).get_columns("mentors")]
    if "group_slugs" not in columns:
        conn.execute(text("ALTER TABLE mentors ADD COLUMN group_slugs TEXT"))


def _rename_category_to_subteam(conn) -> None:
    """On students/mentors, add `subteam_slug`, backfill from the legacy `category`
    column (slug values already match), then drop `category`."""
    from sqlalchemy import inspect, text
    for table in ("students", "mentors"):
        columns = [c["name"] for c in inspect(conn).get_columns(table)]
        if "subteam_slug" not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN subteam_slug VARCHAR(50)"))
            if "category" in columns:
                conn.execute(text(f"UPDATE {table} SET subteam_slug = category"))
        if "category" in [c["name"] for c in inspect(conn).get_columns(table)]:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN category"))


def _drop_is_lead_column(conn) -> None:
    """Drop the retired `is_lead` flag from mentors (lead status is a Legion group now)."""
    from sqlalchemy import inspect, text
    columns = [c["name"] for c in inspect(conn).get_columns("mentors")]
    if "is_lead" in columns:
        conn.execute(text("ALTER TABLE mentors DROP COLUMN is_lead"))


def _add_checkout_requested_at_column(conn) -> None:
    """Add checkout_requested_at to the sessions table if not already present."""
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("sessions")]
    if "checkout_requested_at" not in columns:
        conn.execute(text("ALTER TABLE sessions ADD COLUMN checkout_requested_at DATETIME"))


def _add_archive_columns(conn) -> None:
    """Add is_active / archived_at to students and mentors if not already present.

    Existing rows default to active (is_active = 1). If the legacy `active` column
    exists on a table, copy its values into is_active then drop the redundant column
    (SQLite 3.35+ supports DROP COLUMN; runtime is 3.45+).
    """
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    for table in ("students", "mentors"):
        columns = [c["name"] for c in inspector.get_columns(table)]
        if "is_active" not in columns:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"
            ))
            # Re-read columns after the ALTER so the legacy check below sees is_active.
            columns = [c["name"] for c in inspect(conn).get_columns(table)]
        if "archived_at" not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN archived_at DATETIME"))
        # Drop the old `active` column that existed before is_active was introduced.
        # Preserve any false values first so archived members stay archived.
        if "active" in columns:
            conn.execute(text(
                f"UPDATE {table} SET is_active = active WHERE active != is_active"
            ))
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN active"))


def _make_weekly_req_team_nullable(conn) -> None:
    """Rebuild weekly_requirements so team_id allows NULL (= applies to all teams).

    SQLite can't drop a NOT NULL constraint in place, so recreate the table and
    copy the rows. No-op once team_id is already nullable.
    """
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    cols = {c["name"]: c for c in inspector.get_columns("weekly_requirements")}
    if "team_id" not in cols or cols["team_id"]["nullable"]:
        return

    conn.execute(text("ALTER TABLE weekly_requirements RENAME TO weekly_requirements_old"))
    conn.execute(text(
        """
        CREATE TABLE weekly_requirements (
            id INTEGER NOT NULL,
            team_id INTEGER,
            category VARCHAR(8),
            week_start DATE NOT NULL,
            required_hours FLOAT NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (team_id, category, week_start),
            FOREIGN KEY(team_id) REFERENCES teams (id)
        )
        """
    ))
    conn.execute(text(
        """
        INSERT INTO weekly_requirements (id, team_id, category, week_start, required_hours)
        SELECT id, team_id, category, week_start, required_hours FROM weekly_requirements_old
        """
    ))
    conn.execute(text("DROP TABLE weekly_requirements_old"))


def _weekly_req_category_to_subteam(conn) -> None:
    """Rebuild weekly_requirements renaming `category` → `subteam_slug` and swapping the
    unique constraint to (team_id, subteam_slug, week_start). SQLite can't rename a column
    inside a UNIQUE constraint in place, so recreate + copy (mirrors the nullable rebuild).
    No-op once `subteam_slug` already exists."""
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(conn).get_columns("weekly_requirements")}
    if "subteam_slug" in cols or "category" not in cols:
        return

    conn.execute(text("ALTER TABLE weekly_requirements RENAME TO weekly_requirements_old"))
    conn.execute(text(
        """
        CREATE TABLE weekly_requirements (
            id INTEGER NOT NULL,
            team_id INTEGER,
            subteam_slug VARCHAR(50),
            week_start DATE NOT NULL,
            required_hours FLOAT NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (team_id, subteam_slug, week_start),
            FOREIGN KEY(team_id) REFERENCES teams (id)
        )
        """
    ))
    conn.execute(text(
        """
        INSERT INTO weekly_requirements (id, team_id, subteam_slug, week_start, required_hours)
        SELECT id, team_id, category, week_start, required_hours FROM weekly_requirements_old
        """
    ))
    conn.execute(text("DROP TABLE weekly_requirements_old"))


def _drop_slack_session_columns(conn) -> None:
    """Drop slack_message_ts and slack_channel_id from sessions (abandoned feature)."""
    from sqlalchemy import inspect, text
    columns = [c["name"] for c in inspect(conn).get_columns("sessions")]
    for col in ("slack_message_ts", "slack_channel_id"):
        if col in columns:
            conn.execute(text(f"ALTER TABLE sessions DROP COLUMN {col}"))


async def _seed_teams() -> None:
    from app.models import Team
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Team))
        existing = result.scalars().all()
        existing_numbers = {t.number for t in existing}

        for number, name in [(4143, "MARS/WARS"), (4423, "MARS' Minions")]:
            if number not in existing_numbers:
                session.add(Team(number=number, name=name))
            else:
                # Update name in case it was seeded with an old value
                team = next(t for t in existing if t.number == number)
                team.name = name

        await session.commit()


async def _seed_subteams() -> None:
    """Seed the Legion-default subteams so the app works before its first roster sync.
    The sync (`legion_sync`) later upserts the authoritative set from Legion by slug."""
    from app.models import Subteam
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        existing = {s.slug for s in (await session.execute(select(Subteam))).scalars().all()}
        for order, (slug, label) in enumerate(
            [("software", "Software"), ("design", "Design"), ("business", "Business")]
        ):
            if slug not in existing:
                session.add(Subteam(slug=slug, label=label, sort_order=order))
        await session.commit()
