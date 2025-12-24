#!/bin/bash

# Quick start script for Attendance Tracker
# Activates virtual environment and starts the application

echo "🕐 Starting Attendance Tracker..."

# Check if virtual environment exists
if [ ! -d "../venv" ]; then
    echo "❌ Virtual environment not found. Please run setup.sh first."
    exit 1
fi

# Activate virtual environment
source ../venv/bin/activate

# Check if credentials exist
if [ ! -f "./config/credentials.json" ]; then
    echo "⚠️ Warning: Google Sheets credentials (credentials.json) not found"
    echo "   The app will start but Google Sheets integration won't work"
    echo "   See README.md for setup instructions"
    echo ""
fi

# Start the application
echo "🚀 Starting application..."
python ./app/app.py