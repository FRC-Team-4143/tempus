# Tempus — Codebase Guide

FRC attendance and hours tracker for teams 4143 (MARS/WARS) and 4423 (MARS' Minions). FastAPI + SQLAlchemy (async) + Jinja2 + SQLite. Slack integration for `/hours`, `/shop`, `/edit`, and `/qr` slash commands. A personal portal at `/me` lets any active student or mentor see their own hours on the web, alongside the kiosk (public sign-in display) and `/admin` (staff-only management).

## Running

```bash
source venv/bin/activate
uvicorn app.main:app --reload
```

Requires a `.env` file (see `.env.example`). Key vars: `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `BASE_URL` (Tempus's own public URL, for the `/hours` → `/me` Slack link), and the Legion integration — `SSO_SECRET` (must equal Legion's), `LEGION_BASE_URL`, `LEGION_API_KEY`. There is **no** admin password; `/admin` is gated by Legion SSO + the `tempus-admin` (full) or `tempus-manager` (dashboard + report view only) group. `/me` (the personal portal) is gated by Legion SSO alone — any active student or mentor on the roster gets in, no group required.

## Testing

```bash
pytest
```

Uses in-memory SQLite with async fixtures via `pytest-asyncio`. **Do not mock the database** — tests hit a real (in-memory) DB to catch query bugs.

## Project Layout

```
app/
  main.py          # FastAPI app, router wiring, lifespan
  config.py        # Settings (pydantic-settings, reads .env)
  database.py      # Engine, session, init_db(), hand-written safe migrations
  models.py        # SQLAlchemy ORM models
  utils.py         # Timezone helpers + shared date/time utilities (see below)
  routers/
    kiosk.py       # Student-facing display pages + SSE stream
    admin.py       # Legion-SSO-gated management UI (tempus-admin / tempus-manager groups)
    portal.py      # Personal page at /me — any active student/mentor, no group required
    slack.py       # Slack slash commands (/hours, /shop, /edit, /qr) + interactions
  services/
    attendance.py  # Sign-in/out logic, hours calculation
    sso.py         # Verifies Legion's mw_sso cookie (verify-only; Tempus never mints it)
    legion_sync.py # Pulls the roster from Legion's read-only API into the local mirror
    leads.py       # Lead mentors from tempus-lead-<team>-<subteam> groups
    slack_client.py # Slack API helpers (DMs, file uploads)
    broadcaster.py # SSE event broadcaster for live kiosk updates
    scheduler.py   # APScheduler jobs: auto sign-out, weekly DMs, nightly backup, hourly Legion sync
    requirements.py # Weekly hour requirement resolution (team + subteam)
    audit.py       # Append-only mutation log
    reports.py     # Weekly report generation (team-wide and single-person), default date range
    backup.py      # SQLite snapshot backup
    app_settings.py # Persisted runtime settings (leaderboard cutoff, legion sync watermark, etc.)
```

### Legion integration (source of truth for the roster)
Legion owns members, teams, subteams, and user groups; Tempus is a **read-only consumer** —
data flows Legion → Tempus only, never back.
- **Auth (`services/sso.py`, `routers/admin.py`):** `/admin` verifies Legion's `mw_sso` cookie
  locally with the shared `SSO_SECRET` (no callback) via `_require_auth`, which recognizes two
  tiers: `tempus-admin` (everything) and `tempus-manager` (`_manager_allowed` — only the
  dashboard and report view; anything else 303s back to the dashboard). No local password. On a
  missing/invalid cookie, redirect to `{LEGION_BASE_URL}/sso/authorize?app=tempus`. The audit
  actor is the SSO username.
- **Personal portal (`routers/portal.py`, `/me`):** open to any active student or mentor on the
  roster (matched by the cookie's `member_code`), independent of `/admin` group membership —
  shows recent sessions, a total-hours headline, and their own weekly report table (student
  rows include the requirement met/not-met styling; mentor rows are hours-only, since mentors
  have no weekly requirement). Signed in but no matching active local record → a "not synced
  yet" message rather than a silent bounce. Cross-navigation is trivial since both surfaces read
  the same live `mw_sso` claims (no bridging route): `admin/base.html` always shows a **My
  Tempus** link to `/me`; `portal/base.html` shows an **Admin** link when
  `session_identity(request).groups` intersects `{tempus-admin, tempus-manager}` (both as a
  navbar link and a prominent "Open admin area" card on the dashboard body). Since the kiosk
  lost its old unguarded "Admin" nav link, the actual way anyone reaches `/admin` or `/me` today
  is Legion's own home-page app launcher (`legion/app/services/home.py`), not any link inside
  Tempus itself.
- **One-tap `/enter` (`routers/portal.py`, `services/legion_auth.py`):** the `/hours` Slack
  reply ends with an "open my dashboard" link to `/enter?member=<code>&next=/me`. If the browser
  already holds a live `mw_sso` cookie, `/enter` redirects straight to `next` (no Legion round
  trip — stops repeat clicks from spamming a fresh Slack push); otherwise it calls Legion's
  `POST /sso/challenge` (`X-API-Key`) to start an Approve/Deny push for that member without a
  typed username, then redirects to Legion's `/sso/pending/{nonce}`. Mirrors Munus's `/enter`,
  but passes an **absolute** `return_to` (`{BASE_URL}{next}`) so the fresh-sign-in path lands
  back on Tempus's host — Legion redirects to `return_to` verbatim, and a bare path would
  resolve against Legion's own host. Needs `BASE_URL` set (Tempus's public URL, whose host is
  already in Legion's `SSO_ALLOWED_RETURN_HOSTS`).
- **Roster mirror (`services/legion_sync.py`):** the local `Student`/`Mentor`/`Team`/`Subteam`
  tables are a synced mirror keyed on Legion's stable `member_code`. Sync pulls
  `/api/members?updated_since=…` (+ teams/subteams) hourly and on the **Sync now** button;
  legacy rows are back-linked by `slack_user_id` then name. `AttendanceSession`/`MentorSession`
  FKs stay local. **Never add roster CRUD or write-back to Legion.**
- **Subteams, not a `FocusCategory` enum:** `subteam_slug` (a string, synced from Legion's
  `subteam.slug`) replaced the old enum on Student/Mentor/WeeklyRequirement; a local `Subteam`
  mirror table holds labels for dropdowns.
- **Leads are Legion groups, not a flag:** `Mentor.is_lead` is gone. A mentor leads a student
  when they hold `tempus-lead-<team_number>-<subteam_slug>` (synced into `Mentor.group_slugs`);
  `services/leads.lead_mentors_for_student` is the single source used by both the on-demand and
  scheduled escalation DMs. QR badges / kiosk sign-in key on `member_code` (legacy
  `student_code`/`mentor_code` still accepted).

## Key Conventions

### Datetimes
All datetimes in the database are **naive UTC**. Never store timezone-aware datetimes.
- Convert for display: `utc_to_local(dt)` → naive local time
- Convert for DB queries: `local_to_utc(dt)` → naive UTC
- Get today's local date: `today_local()`
- Both in `app/utils.py`

### Shared Utilities (`app/utils.py`)
Use these instead of inlining the logic:
- `format_elapsed(start, end=None) -> str` — formats elapsed time as `"Xh YYm"`
- `current_week_bounds() -> (week_start_utc, week_end_utc)` — current Mon–Sun week in UTC

### Hours Calculation
Always use `_status_multiplier(status)` in `app/services/attendance.py`. Never hardcode multipliers (contributor=1.0, present=0.5, distraction=0.0 are configurable in settings).

### The "counts hours since" cutoff (`app_settings.leaderboard_since`)
One setting governs every "total hours" display in the app — the kiosk/dashboard
leaderboards, the `/hours` Slack command's season total + rank, and the personal
portal's total-hours headline all filter on `AttendanceSession`/`MentorSession
.sign_in_time >= leaderboard_since_utc(db)` when a cutoff is configured (blank =
all-time). `services/reports.default_report_range(since)` derives the report view's
*default* date-filter window from the same setting: from the Monday of the cutoff's
week through the current week (falling back to a rolling 4-week window when no
cutoff is set) — used by `/admin/report`, `/admin/report/export`, and `/me`.

### Database Migrations
No Alembic. Add a `def _migration_name(conn)` function to `database.py` and call it from `init_db()`. Pattern: check if column/table exists, then apply the change. SQLite 3.35+ `DROP COLUMN` is supported.

## UI Conventions

Two visual styles — keep them separate:

- **Kiosk pages** (`kiosk.html`, `mentor.html`): Custom dark CSS. Background `#0a0a0a`, panels `#111111`, accent red `#cc2200`, borders `#2a1a1a`. No Bootstrap.
- **Admin pages** (extend `admin/base.html`): Bootstrap 5 with kiosk-color overrides. Card/table styles are overridden in `base.html` to match the dark theme — don't add Bootstrap default light classes like `table-light` or `shadow`.
- **Portal pages** (`/me`, extend `portal/base.html`): same Bootstrap-plus-dark-overrides palette as admin, but a simple top navbar instead of the admin sidebar — mirrors Munus's `portal/base.html`.

## Scheduled Jobs (`scheduler.py`)

| Job | Schedule (configurable) |
|-----|------------------------|
| Auto sign-out | Daily at `AUTO_SIGNOUT_TIME` |
| Weekly DMs | `WEEKLY_DM_DAY` at `WEEKLY_DM_TIME` |
| Nightly backup | Daily at `BACKUP_TIME` |
