# App Data Directory

This directory contains all local data for the Attendance Tracking System.

## Files

### `users.csv`
The master user roster file with the following format:
```csv
"Name","TeamNumber","Category","SlackUserID"
"Cole Hunt","4143","Software","U2XB520MA"
```

**Columns:**
- **Name**: Full name of the team member
- **TeamNumber**: FRC team number (4143 or 4423)
- **Category**: Department (Software, Design, Business)
- **SlackUserID**: Slack User ID for notifications (optional)

### `attendance.db`
SQLite database containing all attendance records and user statistics.

**Tables:**
- `attendance_records`: Check-in/check-out records with timestamps
- `user_hours`: Aggregated hours and statistics per user

## Data Management

### Adding/Updating Users
1. Edit `users.csv` directly, or
2. Use the admin interface to upload a new CSV, or
3. Use the API: `POST /api/add-name` or `POST /api/upload-names`

### Backup Recommendations
- Regular backups of `attendance.db` and `users.csv`
- Google Sheets serves as an automatic backup (if configured)
- All data is stored locally first, ensuring no data loss

### Data Privacy
- All data is stored locally on the server
- Google Sheets sync is optional
- Slack IDs are only used if Slack integration is enabled
- No data is sent to external services except Google Sheets and Slack (when configured)

## Offline Mode

All data is stored locally first:
- Works completely offline
- No internet required for core functionality
- Syncs to cloud services when connection is available

See [OFFLINE_MODE.md](../OFFLINE_MODE.md) for details.