# Offline Mode Support

The Attendance Tracking System is designed to work with or without an internet connection.

## How It Works

### Core Functionality (Always Available)
The following features work offline without any internet connection:
- Check-in/Check-out tracking
- Local database storage
- Attendance records
- User status
- Leaderboard
- Admin dashboard
- All local operations

### Internet-Dependent Features
The following features require an internet connection:
- **Google Sheets Sync**: Automatically syncs attendance records to Google Sheets
- **Slack Notifications**: Sends weekly attendance summaries via Slack

## Automatic Connectivity Checks

The system automatically checks for internet connectivity before attempting to use internet-dependent features:

### Google Sheets Sync
- **Background Sync**: Runs every 5 minutes, automatically skipped if offline
- **Manual Sync**: Returns error message if no connection available
- **Graceful Degradation**: Records are stored locally and will sync when connection is restored

### Slack Notifications
- **Scheduled Notifications**: Automatically skipped if offline
- **Manual Notifications**: Returns error message if no connection available
- **Retry Logic**: Notifications can be manually triggered once connection is restored

## Monitoring Connectivity

### Health Check Endpoint
Check the `/health` endpoint to see current connectivity status:

```bash
curl http://localhost:5000/health
```

Response includes:
```json
{
  "status": "healthy",
  "timestamp": "2026-01-04T10:30:00",
  "connectivity": {
    "internet": true,
    "google_sheets": true,
    "slack": true
  },
  "services": {
    "database": true,
    "google_sheets_configured": true,
    "slack_configured": true
  }
}
```

### Log Messages
The application logs connectivity warnings:
- `⚠️ No internet connection, skipping Google Sheets sync`
- `⚠️ No internet connection, cannot send Slack notifications`
- `⚠️ No internet connection, skipping scheduled sync`

## Best Practices

### For Network-Unstable Environments
1. **Local Database**: All attendance data is stored locally first
2. **Automatic Retry**: Background sync will retry automatically when connection is restored
3. **No Data Loss**: Attendance records are never lost due to connectivity issues

### Manual Recovery
If you've been offline for a while:

1. **Check Status**: Visit `/health` to verify connectivity
2. **Manual Sync**: Trigger manual sync via admin panel or API:
   ```bash
   curl -X POST http://localhost:5000/api/manual-sync
   ```
3. **Resend Notifications**: Trigger Slack notifications manually:
   ```bash
   curl -X POST http://localhost:5000/api/slack-notify
   ```

## Configuration

### Disable Internet Features
You can disable internet-dependent features in `config/.env`:

```env
# Disable Slack notifications
SLACK_ENABLED=False

# Adjust sync interval (in minutes)
SYNC_INTERVAL_MINUTES=15
```

### Google Sheets (Optional)
If you don't configure Google Sheets credentials:
- App will work normally for local tracking
- Background sync will be automatically disabled
- No error messages will appear

## Troubleshooting

### "No internet connection" errors
- This is normal when offline
- Core functionality continues to work
- Features will resume automatically when connection is restored

### Sync not working after coming back online
1. Check `/health` endpoint to verify connectivity
2. Manually trigger sync: `POST /api/manual-sync`
3. Check logs for any configuration issues

### Notifications not sending
1. Verify Slack is enabled: `SLACK_ENABLED=True` in `.env`
2. Check connectivity: `/health` endpoint
3. Verify bot token and permissions
4. Check logs for specific Slack API errors
