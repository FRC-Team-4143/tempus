# Google Sheets Integration Setup

This guide will help you set up Google Sheets integration for the Attendance Tracker.

## Prerequisites
- Google Cloud Console account
- Google Sheets spreadsheet created and shared

## Step-by-Step Setup

### 1. Create Google Cloud Project
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one

### 2. Enable Required APIs
1. In your project, go to "APIs & Services" > "Library"
2. Search for and enable:
   - Google Sheets API
   - Google Drive API

### 3. Create Service Account
1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "Service Account"
3. Fill in service account details:
   - Name: "attendance-tracker-service"
   - Description: "Service account for attendance tracking"
4. Click "Create and Continue"
5. Skip role assignment for now
6. Click "Done"

### 4. Generate JSON Key
1. In the "Credentials" page, find your service account
2. Click the three dots menu > "Manage keys"
3. Click "Add Key" > "Create new key"
4. Select "JSON" format
5. Download the file automatically

### 5. Configure Credentials
1. Rename the downloaded JSON file to `credentials.json`
2. Copy `credentials.json` to the `config/` directory
3. The file should be placed at: `config/credentials.json`

### 6. Share Google Sheet
1. Create a new Google Sheet or use existing one
2. Share the sheet with your service account email:
   - The email is found in `credentials.json` as `client_email`
   - Give "Editor" permissions
3. Copy the spreadsheet ID from the URL:
   - URL format: `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`
   - Add to your `.env` file: `SPREADSHEET_NAME=SPREADSHEET_ID`

### 7. Environment Variables
Update your `config/.env` file:
```
SPREADSHEET_NAME=your_spreadsheet_id_here
SECRET_KEY=your_secret_key_here
FLASK_DEBUG=false
```

## Testing
Run the setup script to verify everything works:
```bash
cd config
./setup.sh
```

Then start the application:
```bash
cd ../app
./start.sh
```

## Troubleshooting
- Ensure the service account has Editor access to the Google Sheet
- Verify the spreadsheet ID is correct in `.env`
- Check that all APIs are enabled in Google Cloud Console
- Make sure `credentials.json` is in the `config/` directory

## Security Notes
- Never commit `credentials.json` to version control
- Keep your service account key secure
- Regularly rotate service account keys
- Only give necessary permissions to the service account