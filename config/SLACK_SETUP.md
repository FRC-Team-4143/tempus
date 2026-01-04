# Slack Bot Integration for Attendance Tracking

This document explains how to set up and use the Slack bot integration for automated attendance notifications.

## Overview

The Slack bot integration sends weekly attendance summaries to **all team members** via direct message. Students below 80% season attendance automatically receive group messages that include their category mentor and lead mentor.

## Features

- **Universal Weekly Summaries**: All students receive weekly attendance updates every Sunday at 8 PM (configurable)
- **Condensed Messages**: Clean, concise format showing weekly and season hours with status indicators
- **Mentor Alerts**: Students below 80% season attendance automatically included in group DMs with mentors
- **Multi-Level Escalation**: Category mentor + lead mentor included for at-risk students
- **Status-Based Guidance**: Contextual advice based on attendance level (good/warning/danger)
- **Manual Triggers**: Admin API endpoints to manually send notifications or test the integration
- **Offline Support**: Gracefully handles no internet connection, automatically resumes when online
- **Flexible Scheduling**: Configurable day and time for automated notifications

## Setup Instructions

### 1. Create a Slack App

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps)
2. Click "Create New App" → "From scratch"
3. Name your app (e.g., "FRC Attendance Bot")
4. Select your workspace

### 2. Configure Bot Permissions

1. In your app settings, go to "OAuth & Permissions"
2. Under "Scopes" → "Bot Token Scopes", add these permissions:
   - `chat:write` - Send messages
   - `users:read` - Look up users
   - `im:write` - Send direct messages
   - `mpim:write` - Create group direct messages (required for mentor alerts)
   - `mpim:read` - Read group DM info
3. Click "Install to Workspace" at the top
4. Copy the "Bot User OAuth Token" (starts with `xoxb-`)

**Note**: The `mpim:write` and `mpim:read` permissions are required for mentor group DMs. Without these, mentor alerts will fail.

### 3. Get Slack User IDs

For each team member who should receive notifications, you need their Slack User ID:

1. In Slack, right-click on the user's profile
2. Select "Copy member ID"
3. The ID will look like `U01234567AB`

### 4. Configure the Application

#### Update `.env` file:

```bash
# Copy the example file if you haven't already
cp config/.env.example config/.env

# Edit config/.env and add:
SLACK_BOT_TOKEN=xoxb-your-actual-bot-token-here
SLACK_ENABLED=True

# Optional: Customize notification schedule
SLACK_NOTIFICATION_DAY=6  # 0=Monday, 6=Sunday
SLACK_NOTIFICATION_HOUR=20  # 8 PM in 24-hour format
```

#### Update `data/users.csv`:

Add Slack User IDs as a 4th column:

```csv
"Adric Schonert","4143","Software","U01234567AB"
"Alexander Buhr","4143","Design","U98765432CD"
...
```

Users without a Slack ID will be skipped during notifications.

#### Create `config/mentors.csv`:

Map mentors to team/category combinations:

```csv
"TeamNumber","Category","MentorName","SlackUserID"
"4143","Software","John Doe","U01234567"
"4143","Design","Jane Smith","U89012345"
"4143","Business","Bob Jones","U67890123"
"4423","Software","Alice Brown","U45678901"
"4423","Design","Charlie Davis","U23456789"
"4423","Business","Eve Wilson","U34567890"
"LEAD","ALL","Brian Stoecker","U2QSDQLE5"
```

The special `LEAD/ALL` entry is included in all mentor alerts.

### 5. Install Dependencies

```bash
pip install -r requirements.txt
```

### 6. Start the Application

```bash
python -m app.app
```

The scheduler will automatically start and schedule weekly notifications.

## Usage

### Automatic Notifications

Once configured, the system will automatically:
1. Check weekly attendance every Sunday at 8 PM (or your configured time)
2. Identify users who didn't meet the 11-hour requirement
3. Send personalized DMs to those users

### Manual Triggers

#### Trigger Weekly Notifications Manually

```bash
curl -X POST http://localhost:5000/api/slack-notify
```

Or for a previous week:
```bash
curl -X POST "http://localhost:5000/api/slack-notify?weeks_back=1"
```

#### Send a Test Notification

```bash
curl -X POST http://localhost:5000/api/slack-test \
  -H "Content-Type: application/json" \
  -d '{"name": "Adric Schonert"}'
```

### API Response Format

**Success Response:**
```json
{
  "success": true,
  "message": "Notified 5 users",
  "notified_users": ["User A", "User B", ...],
  "failed_users": [],
  "skipped_users": ["User C"],
  "week_start": "2026-01-05",
  "week_end": "2026-01-11"
}
```

## Notification Message Format

### Students in Good Standing (≥80%)

```
📊 Weekly Attendance Summary

Hi [Name]!

Week of 2026-01-05 to 2026-01-11
• Weekly: 11.0 / 11.0 hrs (100%) - ✅ Meeting Requirements
• Season: 55.0 / 55.0 hrs (100%) - ✅ Meeting Requirements

🎉 Keep up the great work!
```

### Students Below 80% (With Mentor Alert)

```
🔔 Mentor Alerted

Due to current attendance records a mentor has been included on this weeks summary.

📊 Weekly Attendance Summary

Hi [Name]!

Week of 2026-01-05 to 2026-01-11
• Weekly: 8.5 / 11.0 hrs (77%) - ⚠️ Below Target
• Season: 45.2 / 55.0 hrs (82%) - ⚠️ Below Target

⚠️ Try to complete more hours each week to get back on track.
```

**Note**: Mentor alerts are sent as group DMs including the student, their category mentor, and the lead mentor.

## Troubleshooting

### Slack Bot Not Sending Messages

1. **Check Configuration**: Verify `SLACK_ENABLED=True` in your `.env` file
2. **Verify Token**: Make sure your bot token is correct and starts with `xoxb-`
3. **Check Permissions**: Ensure the bot has `chat:write` and `im:write` scopes
4. **Invite Bot**: The bot may need to be invited to your workspace if it was recently created
5. **Check Logs**: Look for error messages in the application logs

### Users Not Receiving Notifications

1. **Verify Slack IDs**: Make sure the user's Slack ID is correct in `data/users.csv`
2. **Check Mentor Configuration**: Verify `config/mentors.csv` exists and is formatted correctly
3. **Check User DM Settings**: The user may have DMs disabled from apps
4. **Network Connection**: Check `/health` endpoint to verify internet connectivity
5. **Invalid User Combination Error**: Usually means:
   - Bot ID accidentally included in recipients list
   - Invalid Slack User ID format (must start with 'U')
   - Duplicate user IDs in the recipient list
   - User has been deactivated in Slack

### Scheduler Not Running

1. **Check Start**: Ensure the scheduler is starting when the app launches (check logs for "Scheduler started")
2. **Time Zone**: The scheduler uses the server's local time zone
3. **APScheduler Issues**: Make sure APScheduler is installed: `pip install APScheduler==3.10.4`

## Configuration Options

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `SLACK_BOT_TOKEN` | - | Your Slack bot token (required) |
| `SLACK_ENABLED` | `False` | Enable/disable Slack notifications |
| `SLACK_NOTIFICATION_DAY` | `6` | Day of week to send notifications (0=Monday, 6=Sunday) |
| `SLACK_NOTIFICATION_HOUR` | `20` | Hour to send notifications (24-hour format) |

## Offline Mode Support

The Slack integration gracefully handles offline scenarios:

- **Automatic Detection**: Checks internet connectivity before sending notifications
- **Scheduled Tasks**: Automatically skipped if offline, no errors logged
- **Manual Triggers**: Return clear error message if offline
- **Auto-Resume**: Works automatically when connection is restored
- **Health Check**: Use `/health` endpoint to check Slack connectivity status

See [OFFLINE_MODE.md](../OFFLINE_MODE.md) for complete offline mode documentation.

## Security Considerations

- **Never commit** your `.env` file with the actual bot token to version control
- Keep your bot token secure and rotate it periodically
- Consider using environment variables or secrets management for production deployments
- The bot can only send DMs to users in the same workspace
- Mentor Slack IDs should be kept up to date in `config/mentors.csv`

## Future Enhancements

Potential improvements for the Slack integration:

- Add support for Slack channels (team-wide announcements)
- Send positive reinforcement messages to users who exceed requirements
- Add weekly summary reports for team leaders
- Support for custom message templates
- Integration with Slack's Block Kit for richer message formatting
- User opt-out preferences

## Support

For issues or questions about the Slack integration:
1. Check the application logs for detailed error messages
2. Verify all configuration steps were completed
3. Test with the `/api/slack-test` endpoint first
4. Review Slack API documentation: [https://api.slack.com/](https://api.slack.com/)
