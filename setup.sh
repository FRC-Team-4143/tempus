#!/bin/bash

# Attendance Tracker Setup Script
# This script helps you get the attendance tracker up and running quickly

echo "🕐 Attendance Tracker Setup Script"
echo "=================================="

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.7 or higher."
    exit 1
fi

echo "✅ Python found: $(python3 --version)"

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    if [ $? -eq 0 ]; then
        echo "✅ Virtual environment created"
    else
        echo "❌ Failed to create virtual environment"
        exit 1
    fi
fi

# Activate virtual environment
echo "🔄 Activating virtual environment..."
source venv/bin/activate

if [ $? -ne 0 ]; then
    echo "❌ Failed to activate virtual environment"
    exit 1
fi

# Install dependencies
echo "📚 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo "✅ Dependencies installed successfully"
else
    echo "❌ Failed to install dependencies"
    exit 1
fi

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo "⚙️ Creating .env configuration file..."
    cp .env.example .env
    echo "✅ .env file created from template"
    echo "📝 Please edit config/.env file with your settings"
else
    echo "✅ .env file already exists"
fi

# Initialize database and user records
echo "🗄️ Initializing database..."
python3 -c "
import sys
sys.path.append('../app')
from database import LocalDatabase
from utils import PRESET_NAMES
db = LocalDatabase()
db.initialize_user_hours(PRESET_NAMES)
print('✅ Database initialized with user records')
"

# Check for credentials file
if [ ! -f "credentials.json" ]; then
    echo "⚠️ Google Sheets credentials file (credentials.json) not found"
    echo "📋 To set up Google Sheets integration:"
    echo "   1. Go to Google Cloud Console (console.cloud.google.com)"
    echo "   2. Create a new project or select existing one"
    echo "   3. Enable Google Sheets API and Google Drive API"
    echo "   4. Create a Service Account with Editor permissions"
    echo "   5. Generate and download JSON key"
    echo "   6. Rename the downloaded file to 'credentials.json'"
    echo "   7. Copy credentials.json to this config/ directory"
    echo "   📄 See credentials.json.example for the expected format"
    echo ""
fi

# Get local IP address
LOCAL_IP=$(hostname -I | cut -d' ' -f1)

echo ""
echo "🎉 Setup complete!"
echo "=================="
echo ""
echo "🚀 To start the attendance tracker:"
echo "   cd ../app"
echo "   ./start.sh"
echo ""
echo "🌐 Access URLs:"
echo "   Local:   http://localhost:5000"
echo "   Network: http://$LOCAL_IP:5000"
echo ""
echo "📱 Share the network URL with other devices on your network"
echo ""
echo "⚙️ Don't forget to:"
echo "   1. Configure your .env file"
echo "   2. Add your Google Sheets credentials.json to config/"
echo "   3. Create and share your Google Sheets spreadsheet"
echo ""
echo "📖 See GOOGLE_SHEETS_SETUP.md for detailed setup instructions"