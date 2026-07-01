# Tempus

A web-based attendance tracking system for FIRST Robotics Competition teams **4143 (MARS/WARS)** and **4423 (MARS' Minions)**. Students sign in and out at a kiosk using QR badges, mentors can edit session ratings via Slack, and an admin UI provides full session management and reporting.

## Features

- **Kiosk sign-in / self sign-out** — QR badge scan signs students in; a second scan signs them out (with a 60-second debounce to prevent accidental double-scans)
- **Slack integration** — mentors edit session ratings via `/edit`, query the current roster with `/shop`, and students check their hours with `/hours`
- **Automated sign-out** — nightly auto sign-out at a configurable time
- **Weekly Slack DMs** — automatic hour-summary messages to students (and mentors if a student is falling behind)
- **Hours multipliers** — session quality ratings (Contributor / Present / Distraction) apply configurable multipliers to counted hours
- **Admin UI** — full CRUD for students, mentors, weekly requirements, and sessions; CSV export; live settings editor
- **Stats & leaderboard** — all-time and weekly hours leaders, longest single session, streak tracking, and team totals on the kiosk

---

## Getting Started

### Prerequisites

- Python 3.11+
- A Slack app with a bot token and signing secret (see [Slack Setup](#slack-setup))

### Installation

```bash
git clone <repo-url>
cd time-tracker
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

### Run (production — systemd on Raspberry Pi)

A systemd unit file is included:

```bash
sudo cp frc-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable frc-tracker
sudo systemctl start frc-tracker
```

The service expects:
- User: `pi`
- Working directory: `/home/pi/time-tracker`
- Environment file: `/home/pi/time-tracker/.env`
- Virtualenv at `/home/pi/time-tracker/venv/`

---

## Configuration Reference

All settings are read from a `.env` file in the working directory (or from environment variables with matching uppercase names).

| Setting | Env Var | Default | Description |
|---|---|---|---|
| `slack_bot_token` | `SLACK_BOT_TOKEN` | *(required for Slack)* | Slack Bot OAuth token (`xoxb-...`) |
| `slack_signing_secret` | `SLACK_SIGNING_SECRET` | *(required for Slack)* | Slack app signing secret for request verification |
| `admin_password` | `ADMIN_PASSWORD` | `changeme` | Password for the `/admin` web UI — **change this** |
| `session_secret` | `SESSION_SECRET` | `dev-secret-change-in-production` | Secret for signing admin session cookies — **change this** |
| `database_url` | `DATABASE_URL` | `sqlite+aiosqlite:///./tracker.db` | SQLAlchemy async database URL; swap for PostgreSQL if needed |
| `timezone` | `TIMEZONE` | `America/New_York` | IANA timezone name used for scheduling and display |
| `auto_signout_time` | `AUTO_SIGNOUT_TIME` | `22:00` | Daily auto sign-out time in 24-hour `HH:MM` format |
| `weekly_dm_day` | `WEEKLY_DM_DAY` | `6` | Day of week for weekly Slack DMs (0 = Monday … 6 = Sunday) |
| `weekly_dm_time` | `WEEKLY_DM_TIME` | `21:00` | Time for weekly Slack DMs in 24-hour `HH:MM` format |
| `signin_ip_whitelist` | `SIGNIN_IP_WHITELIST` | *(unrestricted)* | Comma-separated CIDR ranges allowed to submit sign-ins (e.g. `192.168.1.0/24`); leave blank to allow all |
| `contributor_multiplier` | `CONTRIBUTOR_MULTIPLIER` | `1.0` | Hours multiplier for "Contributor" rated sessions |
| `present_multiplier` | `PRESENT_MULTIPLIER` | `0.5` | Hours multiplier for "Present" rated sessions |
| `distraction_multiplier` | `DISTRACTION_MULTIPLIER` | `0.0` | Hours multiplier for "Distraction" rated sessions |

> **Note:** `contributor_multiplier` applies to both self sign-outs (QR badge rescan) and sessions closed by the nightly auto sign-out job.
>
> Multipliers can be updated at runtime from **Admin → Settings** without restarting the server. Changes are written back to `.env` and take effect immediately.

### Minimal `.env` example

```dotenv
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here
ADMIN_PASSWORD=a-strong-password
SESSION_SECRET=a-long-random-string
TIMEZONE=America/Chicago
AUTO_SIGNOUT_TIME=21:30
```

---

## Slack Setup

1. Create a Slack app at https://api.slack.com/apps
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
5. Install the app to your workspace and copy the **Bot User OAuth Token** and **Signing Secret** to `.env`

Mentors must have their Slack user ID recorded in the admin UI under **Mentors**. Students need their Slack UID set under **Students** to receive DMs.

---

## Admin UI

Navigate to `/admin` and log in with your configured `ADMIN_PASSWORD`. Sessions expire after 12 hours.

| Section | Description |
|---|---|
| **Dashboard** | View currently signed-in students, all-time hours leaderboard, and manually sign in any student |
| **Students** | Create, edit, and delete students; set team, focus category (software / design / business), Slack UID, and active status |
| **Mentors** | Manage mentors with their Slack UIDs and optional team/category for hours-notification matching |
| **Requirements** | Set per-team, per-category, per-week required hours |
| **Sessions** | Filterable and paginated session log; edit individual sessions (recalculates counted hours); CSV export |
| **Settings** | Live-edit the three session-status hour multipliers |

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
│   ├── admin.py       # /admin — password-protected management UI
│   ├── kiosk.py       # / — kiosk display, sign-in endpoint, SSE stream
│   └── slack.py       # /slack — slash commands and interactive button handler
├── services/
│   ├── attendance.py  # Sign-in/sign-out logic and queries
│   ├── broadcaster.py # SSE event broadcaster (asyncio.Queue fan-out)
│   ├── scheduler.py   # APScheduler jobs for auto sign-out and weekly DMs
│   └── slack_client.py# Slack AsyncWebClient wrapper and DM notification logic
└── templates/         # Jinja2 HTML templates
```
