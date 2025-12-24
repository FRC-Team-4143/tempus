#!/usr/bin/env python3
"""
Attendance Tracking System - Main Application
A Flask web application for tracking attendance using local SQLite database with Google Sheets backup.
"""

import os
import logging
from flask import Flask
from dotenv import load_dotenv

# Import modules
from database import LocalDatabase
from routes import (
    index, admin, leaderboard, check_in, check_out, get_status, get_summary,
    get_records, health_check, quick_status, global_status, status_stream,
    get_preset_names, upload_names, add_name, remove_name, toggle_attendance,
    sign_out_all, api_manual_sync, api_user_hours_summary, api_adjust_user_hours
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Initialize database
db = LocalDatabase()

# Initialize user hours for all names from CSV
from utils import PRESET_NAMES
db.initialize_user_hours(PRESET_NAMES)

# Create Flask app
app = Flask(__name__, template_folder='../templates')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Register routes
app.add_url_rule('/', 'index', index, methods=['GET'])
app.add_url_rule('/admin', 'admin', admin, methods=['GET'])
app.add_url_rule('/leaderboard', 'leaderboard', leaderboard, methods=['GET'])
app.add_url_rule('/api/check-in', 'check_in', check_in, methods=['POST'])
app.add_url_rule('/api/check-out', 'check_out', check_out, methods=['POST'])
app.add_url_rule('/api/status/<name>', 'get_status', get_status, methods=['GET'])
app.add_url_rule('/api/summary', 'get_summary', get_summary, methods=['GET'])
app.add_url_rule('/api/records', 'get_records', get_records, methods=['GET'])
app.add_url_rule('/health', 'health_check', health_check, methods=['GET'])
app.add_url_rule('/api/quick-status', 'quick_status', quick_status, methods=['GET'])
app.add_url_rule('/api/global-status', 'global_status', global_status, methods=['GET'])
app.add_url_rule('/api/status-stream/<name>', 'status_stream', status_stream, methods=['GET'])
app.add_url_rule('/api/preset-names', 'get_preset_names', get_preset_names, methods=['GET'])
app.add_url_rule('/api/upload-names', 'upload_names', upload_names, methods=['POST'])
app.add_url_rule('/api/add-name', 'add_name', add_name, methods=['POST'])
app.add_url_rule('/api/remove-name', 'remove_name', remove_name, methods=['POST'])
app.add_url_rule('/api/toggle-attendance', 'toggle_attendance', toggle_attendance, methods=['POST'])
app.add_url_rule('/api/sign-out-all', 'sign_out_all', sign_out_all, methods=['POST'])
app.add_url_rule('/api/manual-sync', 'api_manual_sync', api_manual_sync, methods=['POST'])
app.add_url_rule('/api/user-hours-summary', 'api_user_hours_summary', api_user_hours_summary, methods=['GET'])
app.add_url_rule('/api/adjust-user-hours', 'api_adjust_user_hours', api_adjust_user_hours, methods=['POST'])

if __name__ == '__main__':
    # Run the Flask app
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)