#!/bin/bash

# Demo mode startup script
# Runs the attendance tracker in demo mode (no Google Sheets required)

echo "🎭 Starting Attendance Tracker in Demo Mode..."
echo "   (No Google Sheets credentials required)"

# Install dependencies if virtual environment doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Setting up virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install flask
else
    source venv/bin/activate
fi

# Start demo mode
echo "🚀 Starting demo application..."
python demo.py