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
    echo "✅ Virtual environment created"
fi

# Activate virtual environment
echo "🔄 Activating virtual environment..."
source venv/bin/activate

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
    echo "📝 Please edit .env file with your settings"
else
    echo "✅ .env file already exists"
fi

# Check for credentials file
if [ ! -f "credentials.json" ]; then
    echo "⚠️ Google Sheets credentials file (credentials.json) not found"
    echo "📋 Please follow the setup instructions in README.md to:"
    echo "   1. Create a Google Cloud Project"
    echo "   2. Enable Google Sheets and Drive APIs"
    echo "   3. Create service account credentials"
    echo "   4. Download credentials.json to this directory"
    echo ""
fi

# Get local IP address
LOCAL_IP=$(hostname -I | cut -d' ' -f1)

echo ""
echo "🎉 Setup complete!"
echo "=================="
echo ""
echo "🚀 To start the attendance tracker:"
echo "   source venv/bin/activate  # Activate virtual environment"
echo "   python app.py             # Start the application"
echo ""
echo "🌐 Access URLs:"
echo "   Local:   http://localhost:5000"
echo "   Network: http://$LOCAL_IP:5000"
echo ""
echo "📱 Share the network URL with other devices on your network"
echo ""
echo "⚙️ Don't forget to:"
echo "   1. Configure your .env file"
echo "   2. Add your Google Sheets credentials.json"
echo "   3. Create and share your Google Sheets spreadsheet"
echo ""
echo "📖 See README.md for detailed setup instructions"