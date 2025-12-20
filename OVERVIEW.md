# Attendance Tracking System - Project Overview

## 🎯 Purpose
A web-based attendance tracking system that uses Google Sheets as a backend, designed to be accessed from multiple devices on a private network.

## 🗂️ Project Structure

```
time-tracker/
├── app.py                   # Main Flask application (with Google Sheets)
├── demo.py                  # Demo version (no Google Sheets required)
├── requirements.txt         # Python dependencies
├── setup.sh                 # Setup script with dependencies
├── start.sh                 # Quick start script (full version)
├── demo.sh                  # Quick start script (demo version)
├── .env.example             # Environment variables template
├── .gitignore              # Git ignore file
├── credentials.json.example # Google credentials template
├── README.md               # Detailed documentation
└── templates/
    ├── index.html          # Main attendance interface
    └── admin.html          # Admin dashboard
```

## 🚀 Quick Start Options

### Option 1: Demo Mode (No Setup Required)
```bash
./demo.sh
```
- Runs immediately with mock data
- No Google Sheets configuration needed
- Perfect for testing the interface

### Option 2: Full Setup (Google Sheets Integration)
```bash
./setup.sh          # Install dependencies and setup
# Configure Google Sheets (see README.md)
./start.sh          # Start with real Google Sheets backend
```

## 🌟 Key Features

### For Users
- ✅ **Easy Check-in/Check-out**: Simple form interface
- 📱 **Mobile Friendly**: Responsive design for all devices
- 🔍 **Status Checking**: View current attendance status
- 💾 **Auto-save**: Form data saved locally
- ⏱️ **Duration Tracking**: Automatic time calculation

### For Administrators
- 📊 **Real-time Dashboard**: Live attendance statistics
- 📋 **Record Management**: View all attendance records
- 📥 **Data Export**: Export records to CSV
- 🔄 **Auto-refresh**: Live updates every minute

### Technical Features
- 🌐 **Network Access**: Multi-device support on private networks
- ☁️ **Cloud Storage**: Google Sheets backend for data persistence
- 🎨 **Modern UI**: Bootstrap-based responsive interface
- 🔒 **Data Validation**: Input validation and error handling

## 🎯 Use Cases

- **Small Teams**: Track employee attendance
- **Events**: Monitor participant check-ins
- **Workshops**: Attendance for training sessions
- **Meetings**: Track meeting attendance
- **Volunteer Organizations**: Monitor volunteer hours

## 🔧 Configuration

### Network Access Setup
1. Set `HOST=0.0.0.0` in configuration
2. Find your computer's IP address
3. Share `http://YOUR_IP:5000` with other devices

### Google Sheets Setup
1. Create Google Cloud Project
2. Enable Google Sheets API
3. Create service account credentials
4. Download `credentials.json`
5. Create and share Google Sheets spreadsheet

## 📊 Data Flow

```
User Device → Flask App → Google Sheets → Admin Dashboard
     ↑                                           ↓
     └─────────── Real-time Updates ──────────────┘
```

## 🎨 Interface Preview

### Main Interface
- Clean, professional design
- Large buttons for easy mobile use
- Real-time status display
- Today's attendance summary

### Admin Dashboard
- Statistics cards with key metrics
- Sortable, searchable data table
- Export functionality
- Real-time refresh

## 🔐 Security Notes

- Designed for trusted private networks
- Service account credentials for Google Sheets
- Input validation and sanitization
- CSRF protection via Flask

## 🚀 Next Steps

1. **Try Demo Mode**: Run `./demo.sh` to see the interface
2. **Full Setup**: Follow README.md for Google Sheets integration
3. **Network Deployment**: Configure for multi-device access
4. **Customization**: Modify templates for your organization

## 📞 Support

- Check README.md for detailed instructions
- Run demo mode to test functionality
- Verify network configuration for multi-device access