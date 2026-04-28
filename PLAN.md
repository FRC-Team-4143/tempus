# Plan: FRC Robotics Attendance Tracker

## Key Decisions

- **Stack:** Python FastAPI + SQLite (SQLAlchemy ORM) + Jinja2 templates + Slack Bolt SDK + APScheduler
- **Sign-in:** USB HID card reader (keyboard-emulation) on kiosk browser page
- **Teams:** 4143 and 4423 — shared kiosks (2 devices, same app)
- **Weekly hours:** Start at 11h, adjustable via admin web UI per team per week
- **Just present / messing around:** 50% of elapsed time counted
- **Auto sign-out:** Configurable time (default 10pm), status = `present` (50% hours)
- **Checkout flow:** Mentor types `/checkout <badge_id_or_name>` Slack slash command → bot responds with interactive buttons (Contributor / Just Present)
- **Weekly Slack status:** DM each student with their hours vs. requirement; if behind, add a mentor to the DM as a group DM
- **Admin UI:** Password-protected web pages (session cookie auth)
- **Deployment:** Raspberry Pi, systemd service

---

## Database Models

| Model | Fields |
|---|---|
| `teams` | id, number (4143/4423), name |
| `students` | id, name, badge_id (unique), team_id FK, slack_user_id (nullable), active (bool) |
| `mentors` | id, name, slack_user_id (unique) |
| `weekly_requirements` | id, team_id FK, week_start (date), required_hours (float) |
| `sessions` | id, student_id FK, sign_in_time, sign_out_time (nullable), status (enum: contributor/present/auto), hours_counted (float), slack_message_ts (nullable) |

---

## Project Structure

```
time-tracker/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── routers/
│   │   ├── kiosk.py
│   │   ├── admin.py
│   │   └── slack.py
│   ├── services/
│   │   ├── attendance.py
│   │   ├── slack_client.py
│   │   └── scheduler.py
│   └── templates/
│       ├── base.html
│       ├── kiosk.html
│       └── admin/
│           ├── base.html
│           ├── dashboard.html
│           ├── students.html
│           ├── mentors.html
│           ├── requirements.html
│           ├── sessions.html
│           └── settings.html
├── static/
├── requirements.txt
├── .env.example
├── frc-tracker.service
└── README.md
```

---

## Phase 1 — Foundation

1. Create project structure and `requirements.txt` (fastapi, uvicorn, sqlalchemy, slack-bolt, apscheduler, jinja2, python-dotenv, aiofiles, itsdangerous)
2. `app/config.py` — Pydantic `Settings` class loading from `.env` (Slack bot/signing tokens, admin password, auto sign-out time, weekly DM schedule, DB path, IP whitelist)
3. `app/database.py` — SQLAlchemy engine + session factory (SQLite async)
4. `app/models.py` — ORM models for all five tables above
5. DB init: `create_all()` on startup + seed Teams 4143 and 4423

---

## Phase 2 — Kiosk Sign-in

6. `app/routers/kiosk.py` — endpoints:
   - `GET /kiosk` — full-screen kiosk HTML page showing both teams' currently signed-in students
   - `POST /kiosk/signin` — receives `badge_id`, creates `Session` with `sign_in_time`; rejects duplicate active sessions
   - `GET /kiosk/stream` — Server-Sent Events broadcasting current signed-in list on any change
   - IP-whitelist middleware to restrict sign-ins to the local network (configurable CIDR in `.env`)
7. `app/templates/kiosk.html` — full-screen page with hidden auto-focused `<input>` capturing HID card reader keystrokes (reader sends ID + `\n`). Two-column layout for Team 4143 / Team 4423 with names and sign-in times. Connects to SSE for live updates. Bootstrap 5 styled.
8. `app/services/attendance.py` — `sign_in(badge_id)` and `sign_out(session_id, status)` business logic. `hours_counted` = full duration for `contributor`, 50% for `present`/`auto`.

---

## Phase 3 — Slack Mentor Checkout

9. `app/routers/slack.py` — Slack Bolt app with FastAPI adapter:
   - `/checkout` slash command: looks up student by badge_id or partial name, finds open session, responds with interactive message:
     - **✅ Contributor (full hours)** button
     - **🔸 Present (50% hours)** button
     - Displays student name, team, sign-in time, current elapsed duration
   - `/slack/interact` action handler: mentor clicks button → `sign_out(session_id, status)` → updates message to confirm "✓ [Name] signed out at HH:MM — X.X hrs recorded"
   - Slack request signature verification on all payloads
10. `app/services/slack_client.py` — helpers for sending DMs and opening group conversations (`conversations.open`)

---

## Phase 4 — Scheduler

11. `app/services/scheduler.py` — APScheduler with two jobs:
    - **Auto sign-out job:** Daily at configurable time (default 10pm). Finds all open sessions, calls `sign_out(session_id, status='auto')`.
    - **Weekly status DM job:** Configurable day/time (e.g. Sunday 9pm). For each active student:
      - Sums `hours_counted` for the current week (Mon–Sun)
      - Looks up `WeeklyRequirement` for their team/week (falls back to most recent prior week if none set)
      - If student has `slack_user_id`: sends DM "Week summary: X.X / Y.Y hrs required — ✅ On track" or "⚠️ Behind"
      - If behind: opens a group DM with [student, mentor] via `conversations.open` and posts the status

---

## Phase 5 — Admin Web UI

12. `app/routers/admin.py` — password-protected routes (session cookie via `itsdangerous`):
    - `GET/POST /admin/login` — login form
    - `GET /admin` — dashboard: leaderboard by total hours, currently signed-in count per team, today's sessions
    - `GET/POST /admin/students` — list + add students (name, badge_id, team, slack_user_id, active flag)
    - `GET/POST /admin/students/{id}` — edit / deactivate / delete
    - `GET/POST /admin/mentors` — list + add mentors (name, slack_user_id)
    - `GET/POST /admin/requirements` — weekly hour schedule per team; table view; add/edit rows
    - `GET /admin/sessions` — paginated session history, filterable by student/team/date; inline edit/delete
    - `GET /admin/sessions/export` — CSV download
    - `GET/POST /admin/settings` — auto sign-out time, weekly DM schedule
13. `app/templates/admin/` — Jinja2 + Bootstrap 5 templates for all admin pages

---

## Phase 6 — Deployment

14. `frc-tracker.service` — systemd unit file to run uvicorn on boot
15. `.env.example` — template with all required vars (Slack bot token, signing secret, admin password, DB path, IP whitelist CIDR, auto sign-out time, weekly DM cron)
16. `README.md` — setup steps: create Slack app, install on Pi, configure `.env`, seed students

---

## Verification Checklist

- [ ] Card swipe on kiosk → student appears in signed-in list for correct team within 1 second (SSE update)
- [ ] Duplicate swipe rejected with friendly message on kiosk
- [ ] `/checkout <id>` in Slack → interactive buttons appear with correct student info
- [ ] Clicking Contributor button → full hours recorded, message updates
- [ ] Clicking Present button → 50% hours recorded, message updates
- [ ] Auto sign-out job at configured time → all open sessions close with `auto`/`present` (50%) status
- [ ] Weekly DM job → each student with `slack_user_id` receives hours summary
- [ ] Behind-on-hours student gets group DM including a mentor
- [ ] Admin login gate blocks unauthenticated access (redirect to login)
- [ ] CRUD for students / requirements persists correctly in DB
- [ ] Two kiosk tabs on different IPs both reflect same live signed-in state
- [ ] Weekly requirement falls back to most recent prior week if none set for current week

---

## Further Considerations

1. **Slack public URL:** Slack slash commands require a public-facing URL for the Pi (or an ngrok tunnel during dev). A static IP + router port-forward or a reverse proxy (nginx) is needed for production.
2. **Student Slack ID onboarding:** Students need their Slack user ID linked to their badge in the admin UI for DMs to work. An optional `/link` Slack slash command could automate self-service linking.
3. **`/checkout` name search:** The command supports both badge ID and partial name search (e.g., `/checkout john`) for mentor convenience.
4. **IP whitelist:** Configurable CIDR (e.g., `192.168.1.0/24`) prevents students from signing in remotely. Leave blank to disable.
5. **Hours rollover:** The weekly calculation spans Monday 00:00 – Sunday 23:59 local time. Sessions crossing midnight count toward the day they started.
