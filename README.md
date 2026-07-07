# Tempus

A web-based attendance tracking system for FIRST Robotics Competition teams **4143 (MARS/WARS)** and **4423 (MARS' Minions)**. Students sign in and out at a kiosk using QR badges, mentors can edit session ratings via Slack, and an admin UI provides full session management and reporting.

## Features

- **Kiosk sign-in / self sign-out** — QR badge scan signs students in; a second scan signs them out (with a 60-second debounce to prevent accidental double-scans)
- **Slack integration** — mentors edit session ratings via `/edit`, query the current roster with `/shop`, and students check their hours with `/hours`
- **Automated sign-out** — nightly auto sign-out at a configurable time
- **Weekly Slack DMs** — automatic hour-summary messages to students (and mentors if a student is falling behind)
- **Hours multipliers** — session quality ratings (Contributor / Present / Distraction) apply configurable multipliers to counted hours
- **Admin UI** — Legion-SSO-gated management of weekly requirements and sessions, a read-only roster synced from Legion, CSV export, and a live settings editor
- **Stats & leaderboard** — all-time and weekly hours leaders, longest single session, streak tracking, and team totals on the kiosk

---

## Getting Started

### Prerequisites

- Python 3.11+
- A Slack app with a bot token and signing secret (see [Slack Setup](#slack-setup))

### Installation

```bash
git clone <repo-url>
cd tempus
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

See the full [Configuration Reference](#configuration-reference) below for all available settings.

### Run (development)

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The app will be available at `http://localhost:8000`. On first start, the database is created automatically and seeded with the two teams.

### Run (production — Docker on DigitalOcean)

The app runs as a Docker container alongside Munus behind an nginx reverse proxy.
See the [apps-infra](https://github.com/FRC-Team-4143/apps-infra) repo for the full
deployment setup and first-time server instructions.

Accessible at **http://time.marswars.org**.

Pushing to `main` automatically deploys via GitHub Actions (tests must pass first).

---

## Configuration Reference

All settings are read from a `.env` file in the working directory (or from environment variables with matching uppercase names).

| Setting | Env Var | Default | Description |
|---|---|---|---|
| `slack_bot_token` | `SLACK_BOT_TOKEN` | *(required for Slack)* | Slack Bot OAuth token (`xoxb-...`) |
| `slack_signing_secret` | `SLACK_SIGNING_SECRET` | *(required for Slack)* | Slack app signing secret for request verification |
| `sso_secret` | `SSO_SECRET` | *(required for admin)* | Shared secret for verifying Legion's `mw_sso` cookie — **must equal Legion's `SSO_SECRET`** |
| `sso_session_ttl` | `SSO_SESSION_TTL` | `43200` | Max age (seconds) of the SSO cookie; match Legion |
| `sso_cookie_domain` | `SSO_COOKIE_DOMAIN` | *(none)* | Cookie domain (e.g. `.marswars.org`) so one login spans subdomains |
| `legion_base_url` | `LEGION_BASE_URL` | *(required for admin/sync)* | Base URL of the Legion app (SSO + roster API) |
| `legion_api_key` | `LEGION_API_KEY` | *(required for sync)* | Shared key sent as `X-API-Key` to Legion's roster API — **must equal Legion's `LEGION_API_KEY`** |
| `database_url` | `DATABASE_URL` | `sqlite+aiosqlite:///./tracker.db` | SQLAlchemy async database URL; swap for PostgreSQL if needed |
| `timezone` | `TIMEZONE` | `America/New_York` | IANA timezone name used for scheduling and display |
| `auto_signout_time` | `AUTO_SIGNOUT_TIME` | `22:00` | Daily auto sign-out time in 24-hour `HH:MM` format |
| `weekly_dm_day` | `WEEKLY_DM_DAY` | `6` | Day of week for weekly Slack DMs (0 = Monday … 6 = Sunday) |
| `weekly_dm_time` | `WEEKLY_DM_TIME` | `21:00` | Time for weekly Slack DMs in 24-hour `HH:MM` format |
| `signin_ip_whitelist` | `SIGNIN_IP_WHITELIST` | *(unrestricted)* | Comma-separated CIDR ranges allowed to submit sign-ins (e.g. `192.168.1.0/24`); leave blank to allow all |
| `contributor_multiplier` | `CONTRIBUTOR_MULTIPLIER` | `1.0` | Hours multiplier for "Contributor" rated sessions |
| `present_multiplier` | `PRESENT_MULTIPLIER` | `0.5` | Hours multiplier for "Present" rated sessions |
| `distraction_multiplier` | `DISTRACTION_MULTIPLIER` | `0.0` | Hours multiplier for "Distraction" rated sessions |
| `backup_time` | `BACKUP_TIME` | `23:30` | Nightly SQLite backup time in 24-hour `HH:MM` format |
| `backup_keep` | `BACKUP_KEEP` | `14` | Number of nightly snapshots to retain |
| `backup_dir` | `BACKUP_DIR` | `backups` | Directory for SQLite snapshot files |
| `updates_enabled` | `UPDATES_ENABLED` | `true` | Master switch for automated Slack messages, memes, and scheduled DMs |
| `roast_enabled` | `ROAST_ENABLED` | `false` | Enable "Wall of Shame" memes posted when the nightly job closes forgotten sessions |
| `slack_announce_channel` | `SLACK_ANNOUNCE_CHANNEL` | *(none)* | Slack channel ID the Wall of Shame memes are posted to |

> **Note:** `contributor_multiplier` applies to both self sign-outs (QR badge rescan) and sessions closed by the nightly auto sign-out job.
>
> Most non-secret settings — the multipliers, scheduling times, timezone, backup settings, IP whitelist, and the Wall of Shame options — can be updated at runtime from **Admin → Settings** without restarting the server. Changes are written back to `.env` and take effect immediately. API keys/secrets (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SSO_SECRET`, `LEGION_API_KEY`) are intentionally **not** editable from the UI.

### Minimal `.env` example

```dotenv
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here
SSO_SECRET=must-match-legions-sso-secret
LEGION_BASE_URL=https://legion.example.org
LEGION_API_KEY=must-match-legions-api-key
TIMEZONE=America/Chicago
AUTO_SIGNOUT_TIME=21:30
```

---

## Slack Setup

1. Create a Slack app at https://api.slack.com/apps — **in production this is actually
   the same app shared with Munus and Legion** (see the note below), but the steps to
   create one from scratch are the same either way.
2. Under **OAuth & Permissions**, add these bot scopes:
   - `chat:write`
   - `im:write`
   - `mpim:write`
   - `commands`
3. Add slash commands (all point to the same URL):
   - `/hours` → `https://<your-host>/slack/command`
   - `/edit` → `https://<your-host>/slack/command`
   - `/shop` → `https://<your-host>/slack/command`
4. Under **Interactivity & Shortcuts**, set the Request URL to `https://<your-host>/slack/interact`
   — see the note below if this app is shared with the sibling apps.
5. Install the app to your workspace and copy the **Bot User OAuth Token** and **Signing Secret** to `.env`

> **Sharing one Slack app across Tempus/Munus/Legion:** sending messages and slash
> commands work fine shared (any number of services can use the same bot token, and
> each slash command has its own independently configurable Request URL) — but Slack
> allows only **one** Interactivity Request URL per app, and each of these three
> services wants its own button clicks. Rather than each getting a separate app, the
> shared app's Interactivity Request URL points at Legion's `/slack/dispatch` (a
> stateless relay with no business logic — see `legion/README.md` "Single sign-on"),
> which forwards each payload to whichever app's own `/slack/interact` actually owns
> it based on `action_id`/`callback_id`. Don't point this app's Interactivity URL at
> Tempus's own `/slack/interact` directly if it's the shared app — that would starve
> Munus's and Legion's interactive buttons of real traffic.

Slack user IDs come from **Legion** (the roster source of truth) via the sync — mentors and
students need their Slack UID set in Legion to receive DMs.

---

## Admin UI

Navigate to `/admin`. Access is gated by **Legion SSO** — you're redirected to Legion to sign
in (a Slack Approve/Deny push, no password), and you must hold the **`tempus-admin`** group in
Legion. There is no local admin password; grant the first admin `tempus-admin` in Legion's
`/admin/groups`. The signed-in identity (Legion username) is recorded as the audit actor, and
the session lasts as long as the shared `mw_sso` cookie (12h).

| Section | Description |
|---|---|
| **Dashboard** | View currently signed-in students, all-time hours leaderboard, and manually sign in any student |
| **Roster** | Read-only view of the members synced from Legion (students & mentors, team, subteam, lead groups, link status), a **Sync now** button, and QR-badge sending. Add/edit/archive members in Legion, not here |
| **Requirements** | Set per-team, per-subteam, per-week required hours (subteams come from Legion) |
| **Sessions** | Filterable and paginated session log; edit individual sessions (recalculates counted hours); CSV export |
| **Settings** | Live-edit non-secret configuration — hour multipliers, auto sign-out / weekly DM / backup times, timezone, IP whitelist, Wall of Shame meme options, and the leaderboard season cutoff. Changes write back to `.env` and apply immediately |

### Legion integration

Tempus is a **Legion consumer**: Legion owns the roster (members, teams, subteams, user groups)
and Tempus mirrors it read-only.

- **Auth** — `/admin` verifies Legion's `mw_sso` cookie locally with the shared `SSO_SECRET` and
  checks for the `tempus-admin` group. Add Tempus's host to Legion's `SSO_ALLOWED_RETURN_HOSTS`.
- **Roster sync** — an hourly job (and the **Sync now** button) pulls `GET /api/members?updated_since=…`
  plus teams/subteams, keyed on Legion's stable `member_code`, and upserts the local mirror.
  QR badges and kiosk sign-in use `member_code` (legacy `student_code` badges still work).
- **Leads** — a mentor is "looped in" for a student's escalation DM when they hold the student's
  `tempus-lead-<team_number>-<subteam_slug>` group in Legion (e.g. `tempus-lead-4143-software`).
  Create those groups in Legion's `/admin/groups`.

---

## Kiosk

The kiosk page (`/kiosk`) is designed for a dedicated touchscreen display. It shows currently signed-in students grouped by team with elapsed sign-in time and auto-refreshes via Server-Sent Events — no polling required.

- **Sign-in:** scan a QR badge — the scanner acts as a keyboard and submits the student's tracker UID to `POST /kiosk/signin`
- **Self sign-out:** scanning the same badge again signs the student out (minimum 60 seconds must have elapsed to prevent accidental double-scans)
- **Default sign-out status:** self sign-outs are recorded as **Contributor** (full hours); a mentor can adjust this later with `/edit`
- **Demo mode:** `/kiosk/demo` renders a realistic preview with fake data — useful for layout testing
- **IP whitelist:** set `SIGNIN_IP_WHITELIST` to restrict which network addresses can submit sign-ins (the kiosk device's subnet, for example)

---

## Slack Workflow

### Student sign-in / sign-out

1. Student scans their QR badge at the kiosk → signed in
2. Student scans their badge again when leaving → signed out as **Contributor** (full hours)
3. If the contribution level needs adjusting, a mentor runs `/edit <student name>` in Slack
4. The mentor receives a DM listing the student's last 5 sessions; selecting one shows three rating buttons: **Contributor**, **Present**, **Distraction**
5. Clicking a button updates the session's counted hours and confirms in the DM thread

### Roster lookup

- `/shop` — shows all currently signed-in students, grouped by team with elapsed time
- `/shop 4143` or `/shop 4423` — filters to a single team

### Hours queries

- Students run `/hours` in any Slack channel to get a private summary of their hours this week and for the season vs. their requirement

### Weekly summary DMs

On the configured day and time (`WEEKLY_DM_DAY` / `WEEKLY_DM_TIME`), the scheduler sends every active, Slack-linked student a summary of their weekly hours vs. requirement. Students who are **behind** receive a group DM that also includes any mentors matching their team and focus category.

---

## Database

SQLite is used by default (`tracker.db` in the working directory). No manual schema creation is needed — tables are created on first startup.

To use PostgreSQL, set `DATABASE_URL` to an async-compatible URL:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@host/dbname
```

---

## Project Structure

```
app/
├── main.py            # FastAPI app setup, startup/shutdown hooks
├── config.py          # Pydantic-settings configuration
├── database.py        # Async SQLAlchemy engine, session factory, init_db
├── models.py          # ORM models (Team, Student, Mentor, AttendanceSession, …)
├── schemas.py         # Pydantic request/response schemas
├── utils.py           # Timezone helpers (utc_to_local, local_to_utc, today_local)
├── routers/
│   ├── admin.py       # /admin — Legion-SSO-gated management UI (tempus-admin group)
│   ├── kiosk.py       # / — kiosk display, sign-in endpoint, SSE stream
│   └── slack.py       # /slack — slash commands and interactive button handler
├── services/
│   ├── attendance.py  # Sign-in/sign-out logic and queries
│   ├── sso.py         # Verifies Legion's mw_sso cookie (verify-only consumer)
│   ├── legion_sync.py # Pulls the roster from Legion's read-only API into the local mirror
│   ├── leads.py       # Resolves lead mentors from tempus-lead-<team>-<subteam> groups
│   ├── broadcaster.py # SSE event broadcaster (asyncio.Queue fan-out)
│   ├── scheduler.py   # APScheduler jobs (auto sign-out, weekly DMs, hourly Legion sync)
│   └── slack_client.py# Slack AsyncWebClient wrapper and DM notification logic
└── templates/         # Jinja2 HTML templates
```

## Legion rework — done

The migration to Legion (SSO auth via the `tempus-admin` group, a read-only roster synced
from Legion's API, subteam-driven requirements, and lead escalation via
`tempus-lead-<team>-<subteam>` groups) is complete. See **Admin UI → Legion integration**
above. The one external step is operational: add Tempus's host to Legion's
`SSO_ALLOWED_RETURN_HOSTS` and create the `tempus-admin` / `tempus-lead-*` groups in Legion
