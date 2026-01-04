# FRC Attendance Tracking System

A comprehensive web-based attendance tracking system for FIRST Robotics Competition teams, featuring local-first data storage with optional cloud sync and automated notifications.

## Features

### Core Functionality
- **Real-time Check-in/Check-out**: Simple web interface for attendance tracking
- **Local Database**: SQLite-based storage ensures data is never lost
- **Multi-device Support**: Access from any device on your network
- **Automatic Calculations**: Tracks hours, percentages, and season totals
- **Weekly Metrics**: Monitors progress against weekly hour requirements
- **Leaderboard**: Displays top performers and attendance stats
- **Admin Dashboard**: Manage users, adjust hours, and view analytics

### Cloud Integration (Optional)
- **Google Sheets Sync**: Automatic background sync every 5 minutes
- **Slack Notifications**: Weekly attendance summaries with mentor alerts
- **Offline Mode**: Full functionality without internet, syncs when reconnected

### Automated Features
- **Midnight Sign-out**: Automatically signs out all users at midnight
- **Weekly Notifications**: Scheduled Slack messages every Sunday at 8 PM
- **Mentor Escalation**: Group DMs with mentors for students below 80% attendance
- **Smart Scheduling**: Configurable notification times and frequencies

## Quick Start

### 1. Prerequisites
- Python 3.8 or higher
- Git
- (Optional) Google Cloud account for Sheets integration
- (Optional) Slack workspace for notifications

### 2. Installation

```bash
# Clone the repository
cd /path/to/project

# Run setup script
cd config
./setup.sh
```

The setup script will:
- Create a Python virtual environment
- Install all dependencies
- Initialize the database
- Create configuration files

### 3. Configuration

#### Basic Setup (Required)
Edit `config/.env`:
```env
SECRET_KEY=your-secret-key-here
PORT=5000
EXPECTED_HOURS_START_DATE=2026-01-05
EXPECTED_HOURS_END_DATE=2026-03-14
EXPECTED_HOURS_WEEKLY_INCREASE=11
```

#### User Roster
Add your team members to `data/users.csv`:
```csv
"Name","TeamNumber","Category","SlackUserID"
"Cole Hunt","4143","Software","U2XB520MA"
"Jane Doe","4423","Design",""
```

### 4. Start the Application

```bash
# From the project root
./start.sh
```

Access the application:
- **Local**: http://localhost:5000
- **Network**: http://YOUR_IP:5000 (shown in terminal output)

## Usage

### For Team Members
1. Navigate to the attendance tracker URL
2. Find your name in the list
3. Click to check in when you arrive
4. Click again to check out when you leave
5. View your stats on the leaderboard

### For Admins
- Access admin dashboard at `/admin`
- Upload new user lists
- Manually adjust hours
- Sign out all users at end of day
- Trigger manual Google Sheets sync
- Send test Slack notifications

## Optional Integrations

### Google Sheets Sync
Automatically backup attendance records to Google Sheets.

**Setup Steps:**
1. Create Google Cloud project
2. Enable Google Sheets API and Google Drive API
3. Create service account and download credentials
4. Follow detailed instructions in [docs/GOOGLE_SHEETS_SETUP.md](./docs/GOOGLE_SHEETS_SETUP.md)

### Slack Notifications
Send weekly attendance summaries and mentor alerts.

**Setup Steps:**
1. Create Slack app with bot permissions
2. Configure bot token in `.env`
3. Add Slack user IDs to `data/users.csv`
4. Create `config/mentors.csv` for mentor mappings
5. Follow detailed instructions in [docs/SLACK_SETUP.md](./docs/SLACK_SETUP.md)

## Documentation

- **[docs/OFFLINE_MODE.md](./docs/OFFLINE_MODE.md)** - Complete guide to offline capabilities and connectivity handling
- **[docs/GOOGLE_SHEETS_SETUP.md](./docs/GOOGLE_SHEETS_SETUP.md)** - Step-by-step Google Sheets integration
- **[docs/SLACK_SETUP.md](./docs/SLACK_SETUP.md)** - Slack bot setup and configuration
- **[data/DATA.md](./data/DATA.md)** - Data structure and file formats

## Project Structure

```
time-tracker/
├── app/
│   ├── app.py              # Main Flask application
│   ├── routes.py           # API endpoints and route handlers
│   ├── database.py         # SQLite database management
│   ├── slack_notifier.py   # Slack integration
│   ├── scheduler.py        # Automated task scheduling
│   ├── connectivity.py     # Internet connectivity checks
│   └── utils.py            # Helper functions
├── config/
│   ├── .env                # Configuration (create from .env.example)
│   ├── credentials.json    # Google Sheets credentials (optional)
│   └── mentors.csv         # Mentor mappings for Slack alerts
├── data/
│   ├── users.csv           # Team member roster
│   └── attendance.db       # SQLite database (auto-created)
├── templates/
│   ├── index.html          # Main attendance interface
│   ├── admin.html          # Admin dashboard
│   └── scoreboard.html     # Leaderboard view
├── setup.sh                # Initial setup script
├── start.sh                # Application launcher
└── requirements.txt        # Python dependencies
```

## API Endpoints

### Attendance Operations
- `POST /api/check-in` - Check in a user
- `POST /api/check-out` - Check out a user
- `POST /api/toggle-attendance` - Toggle user status
- `GET /api/status/<name>` - Get user's current status
- `GET /api/global-status` - Get all users' status

### Admin Operations
- `POST /api/upload-names` - Upload new user roster
- `POST /api/adjust-user-hours` - Manually adjust user hours
- `POST /api/sign-out-all` - Sign out all checked-in users
- `GET /api/user-hours-summary` - Get hours summary for all users

### Sync & Notifications
- `POST /api/manual-sync` - Trigger Google Sheets sync
- `POST /api/slack-notify` - Send Slack notifications
- `POST /api/slack-test` - Test Slack integration
- `GET /health` - System health and connectivity status

### Analytics
- `GET /api/weekly-attendance` - Get weekly attendance metrics
- `GET /api/records` - Retrieve attendance records with filters

## Offline Mode

The system is designed to work seamlessly with or without internet:

- ✅ **Always Available**: Check-in/out, database, leaderboard, admin functions
- 🌐 **Internet Required**: Google Sheets sync, Slack notifications
- 🔄 **Auto-Recovery**: Syncs automatically when connection is restored

See [OFFLINE_MODE.md](OFFLINE_MODE.md) for complete details.

## Monitoring & Health

Check system status:
```bash
curl http://localhost:5000/health
```

Returns connectivity status for:
- Database (always available)
- Internet connection
- Google Sheets API
- Slack API

## Troubleshooting

### Application won't start
- Verify virtual environment: `source ../venv/bin/activate`
- Check Python version: `python --version` (3.8+)
- Reinstall dependencies: `cd config && ./setup.sh`

### Google Sheets not syncing
- Verify credentials in `config/credentials.json`
- Check spreadsheet is shared with service account
- Verify internet connectivity: `curl http://localhost:5000/health`
- Check logs for specific errors

### Slack notifications failing
- Verify `SLACK_ENABLED=True` in `.env`
- Check bot token is correct
- Ensure all required permissions are granted
- Verify Slack user IDs in `data/users.csv`
- Test with: `curl -X POST http://localhost:5000/api/slack-test`

### Users not appearing
- Check `data/users.csv` format (4 columns, quoted values)
- Restart application to reload user list
- Verify database initialized: Check for `data/attendance.db`

## Development

### Technology Stack
- **Backend**: Flask (Python)
- **Database**: SQLite
- **Frontend**: HTML, CSS, JavaScript
- **Cloud**: Google Sheets API, Slack API
- **Scheduling**: APScheduler

### Running in Development
```bash
# Activate virtual environment
source ../venv/bin/activate

# Run with debug mode
export FLASK_DEBUG=true
python -m app.app
```

## Security Notes

- **Never commit** `config/.env` or `config/credentials.json` to version control
- Keep Slack bot tokens secure
- Regularly backup `data/attendance.db`
- Use strong `SECRET_KEY` in production
- Run on trusted networks only
---

**Built with ❤️ for the MARS/WARS Robotics Program with lots of help from Copilot**
