# Attendance Tracking System

A modern web-based attendance tracking system that uses Google Sheets as a backend for data storage. Perfect for small teams, organizations, or events that need a simple way to track attendance from multiple devices on a network.

## Features

- **Web-based Interface**: Access from any device with a web browser
- **Google Sheets Backend**: All data is stored in Google Sheets for easy access and backup
- **Real-time Tracking**: Check-in and check-out with automatic timestamp recording
- **Admin Dashboard**: View attendance records, statistics, and export data
- **Responsive Design**: Works on desktop, tablet, and mobile devices
- **Network Access**: Can be accessed by multiple devices on the same network
- **Data Export**: Export attendance records to CSV format
- **User Status**: Check current attendance status and view today's records
- **Duration Calculation**: Automatic calculation of time spent when checking out

## Quick Start

### Prerequisites

- Python 3.7 or higher
- A Google account for Google Sheets integration
- Network access between devices (for multi-device usage)

### Installation

1. **Clone or download this project**:
   ```bash
   git clone <repository-url>
   cd time-tracker
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up Google Sheets integration** (see detailed instructions below)

4. **Configure environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

5. **Run the application**:
   ```bash
   python app.py
   ```

6. **Access the application**:
   - Open your web browser and go to `http://localhost:5000`
   - For network access, use your computer's IP address: `http://192.168.1.100:5000`

## Google Sheets Setup

### Step 1: Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google Sheets API and Google Drive API:
   - Go to "APIs & Services" > "Library"
   - Search for "Google Sheets API" and enable it
   - Search for "Google Drive API" and enable it

### Step 2: Create Service Account Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "Service Account"
3. Enter a name for your service account (e.g., "attendance-tracker")
4. Skip the optional steps and click "Done"
5. Click on your newly created service account
6. Go to the "Keys" tab
7. Click "Add Key" > "Create new key"
8. Choose "JSON" format and click "Create"
9. Save the downloaded JSON file as `credentials.json` in your project directory

### Step 3: Create the Spreadsheet

1. Go to [Google Sheets](https://sheets.google.com/)
2. Create a new spreadsheet
3. Name it "Attendance Tracker" (or whatever you set in SHEET_NAME)
4. Share the spreadsheet with your service account email:
   - Click "Share" in the top right
   - Add the service account email (found in your credentials.json file)
   - Give it "Editor" permissions

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

```env
# Required
SECRET_KEY=your-super-secret-key-change-this-in-production
GOOGLE_CREDENTIALS_PATH=credentials.json
SHEET_NAME=Attendance Tracker

# Network Configuration
HOST=0.0.0.0  # Allows access from other devices
PORT=5000

# Development
DEBUG=False  # Set to True for development
```

### Network Access

To allow access from other devices on your network:

1. Make sure `HOST=0.0.0.0` in your `.env` file
2. Find your computer's IP address:
   - Windows: Run `ipconfig` in Command Prompt
   - Mac/Linux: Run `ifconfig` or `ip addr show`
3. Other devices can access the app at `http://YOUR_IP_ADDRESS:5000`

## Usage

### For Users

1. **Check In**:
   - Enter your name and email
   - Add optional notes
   - Click "Check In"

2. **Check Out**:
   - Enter your name and email (same as check-in)
   - Add optional notes
   - Click "Check Out"

3. **Check Status**:
   - Enter your email
   - Click "Check My Status" to see if you're currently checked in

### For Administrators

1. **Access Admin Dashboard**:
   - Click "Admin Dashboard" link on the main page
   - Or go directly to `/admin`

2. **View Statistics**:
   - See today's check-ins, check-outs, and unique users
   - View recent activity and detailed records

3. **Export Data**:
   - Click "Export CSV" to download attendance records

## Data Structure

The system creates a Google Sheets spreadsheet with the following columns:

| Column | Description |
|--------|-------------|
| Timestamp | Full date and time of the action |
| Name | User's full name |
| Email | User's email address (used as unique identifier) |
| Action | "check-in" or "check-out" |
| Date | Date only (YYYY-MM-DD) |
| Check-in Time | Time of check-in (HH:MM:SS) |
| Check-out Time | Time of check-out (HH:MM:SS) |
| Duration (hours) | Hours worked (calculated on check-out) |
| Notes | Optional notes from user |
| Device IP | IP address of the device used |

## Security Considerations

1. **Credentials**: Keep your `credentials.json` file secure and never commit it to version control
2. **Secret Key**: Use a strong, unique secret key in production
3. **Network Security**: This system is designed for trusted networks. For internet deployment, add authentication
4. **HTTPS**: For production deployment, use HTTPS to encrypt data transmission

## Deployment Options

### Local Network (Recommended for most use cases)

```bash
# Run on all network interfaces
python app.py
```

Access from any device on the network using the host computer's IP address.

### Cloud Deployment (Advanced)

For deployment on platforms like Heroku, DigitalOcean, or AWS:

1. Set environment variables on your platform
2. Upload service account credentials securely
3. Configure HTTPS
4. Consider adding authentication for public access

## Troubleshooting

### Common Issues

1. **"Google Sheets not initialized" error**:
   - Check that `credentials.json` exists and is valid
   - Verify the service account has access to the spreadsheet
   - Ensure Google Sheets API is enabled

2. **Can't access from other devices**:
   - Check that `HOST=0.0.0.0` in your configuration
   - Verify devices are on the same network
   - Check firewall settings on the host computer

3. **"Already checked in" error**:
   - The system prevents multiple check-ins on the same day
   - Check out first, then check in again if needed
   - Or check your current status

### Logs

The application logs important information. Check the console output for error messages and troubleshooting information.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is open source. Feel free to modify and distribute according to your needs.

## Support

For issues and questions:

1. Check the troubleshooting section above
2. Review the Google Sheets API documentation
3. Check the Flask documentation for web framework issues

## Future Enhancements

Potential improvements for future versions:

- [ ] User authentication and authorization
- [ ] Email notifications for administrators
- [ ] QR code generation for easy mobile access
- [ ] Advanced reporting and analytics
- [ ] Integration with other calendar systems
- [ ] Mobile app companion
- [ ] Bulk import/export features
- [ ] Custom fields and categories