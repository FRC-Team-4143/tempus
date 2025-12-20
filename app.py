#!/usr/bin/env python3
"""
Attendance Tracking System
A Flask web application for tracking attendance using Google Sheets as backend.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import gspread
from google.oauth2.service_account import Credentials
from typing import Dict, List, Optional
import hashlib
from dotenv import load_dotenv

# Add a simple in-memory cache to prevent double requests
from threading import Lock
import time

# Request tracking to prevent double-clicks
request_cache = {}
request_lock = Lock()

def is_duplicate_request(key: str, window_seconds: int = 3) -> bool:
    """Check if this is a duplicate request within the time window"""
    with request_lock:
        current_time = time.time()
        
        # Clean old entries
        expired_keys = [k for k, timestamp in request_cache.items() 
                       if current_time - timestamp > window_seconds]
        for k in expired_keys:
            del request_cache[k]
        
        # Check if request is duplicate
        if key in request_cache:
            return True
        
        # Add new request
        request_cache[key] = current_time
        return False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')

# Preset names list - customize this for your team
PRESET_NAMES = [
    "John Doe",
    "Jane Smith", 
    "Mike Johnson",
    "Sarah Wilson",
    "Alex Brown",
    "Emily Davis",
    "Chris Miller",
    "Lisa Garcia",
    "David Lee",
    "Maria Rodriguez"
]

def load_names_from_file():
    """Load names from file if it exists"""
    global PRESET_NAMES
    try:
        if os.path.exists('names_list.csv'):
            with open('names_list.csv', 'r', encoding='utf-8') as f:
                file_names = []
                for line in f:
                    name = line.strip().strip('"')
                    if name:
                        file_names.append(name)
                if file_names:
                    PRESET_NAMES = file_names
                    logger.info(f'Loaded {len(file_names)} names from names_list.csv')
    except Exception as e:
        logger.warning(f'Could not load names from file: {e}')

# Load names on startup
load_names_from_file()

# Google Sheets configuration
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

class AttendanceTracker:
    def __init__(self):
        self.gc = None
        self.sheet = None
        self.worksheet = None
        self.initialize_google_sheets()
    
    def initialize_google_sheets(self):
        """Initialize Google Sheets connection"""
        try:
            # Load credentials from service account file
            creds_path = os.environ.get('GOOGLE_CREDENTIALS_PATH', 'credentials.json')
            if not os.path.exists(creds_path):
                logger.error(f"❌ Credentials file not found: {creds_path}")
                logger.info("Please ensure credentials.json exists in the project directory")
                raise Exception(f"Missing credentials file: {creds_path}")

            creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
            self.gc = gspread.authorize(creds)
            
            # Open existing spreadsheet (DO NOT CREATE)
            sheet_name = os.environ.get('SHEET_NAME', 'Test Attendance Tracker')
            try:
                self.sheet = self.gc.open(sheet_name)
                logger.info(f"✅ Successfully opened existing spreadsheet: {sheet_name}")
            except gspread.SpreadsheetNotFound:
                logger.error(f"❌ Spreadsheet '{sheet_name}' not found or not shared with service account")
                logger.error("📋 Required setup:")
                logger.error("1. Create a Google Sheet manually")
                logger.error(f"2. Name it exactly: '{sheet_name}'")
                logger.error("3. Share with: attendance-tracker@test-attendance-tracker.iam.gserviceaccount.com")
                logger.error("4. Give 'Editor' permissions")
                raise Exception(f"Spreadsheet '{sheet_name}' not accessible")
            
            # Get or create main worksheet
            try:
                self.worksheet = self.sheet.worksheet('Attendance')
                logger.info("Found existing 'Attendance' worksheet")
            except gspread.WorksheetNotFound:
                logger.info("Creating 'Attendance' worksheet in existing spreadsheet")
                self.worksheet = self.sheet.add_worksheet(title='Attendance', rows=1000, cols=10)
                self._setup_headers()
                logger.info("Set up headers in new worksheet")
            
            logger.info("✅ Google Sheets connection established successfully")
                
        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Sheets: {e}")
            logger.error("🚫 Application cannot continue without Google Sheets access")
            # Set worksheet to None to indicate failure
            self.worksheet = None
            self.sheet = None
            self.gc = None
    
    def _setup_headers(self):
        """Setup headers in the spreadsheet"""
        headers = [
            'Timestamp', 'Name', 'Email', 'Action', 'Date', 
            'Check-in Time', 'Check-out Time', 'Duration (hours)', 'Notes', 'Device IP'
        ]
        self.worksheet.append_row(headers)
    
    def add_attendance_record(self, name: str, email: str, action: str, notes: str = '', device_ip: str = ''):
        """Add an attendance record to the sheet"""
        if not self.worksheet:
            logger.error("❌ Google Sheets not available - cannot add record")
            return False, "Google Sheets connection failed. Please check setup and restart the application."
        
        try:
            now = datetime.now()
            timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
            date = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M:%S')
            
            # Check for existing record today
            existing_records = self.get_user_records_today(email)
            duration = ''
            
            if action == 'check-out' and existing_records:
                # Calculate duration
                for record in existing_records:
                    if record.get('Action') == 'check-in' and not record.get('Check-out Time'):
                        check_in_time = datetime.strptime(f"{date} {record['Check-in Time']}", '%Y-%m-%d %H:%M:%S')
                        duration = round((now - check_in_time).total_seconds() / 3600, 2)
                        break
            
            row_data = [
                timestamp, name, email, action, date,
                time_str if action == 'check-in' else '',
                time_str if action == 'check-out' else '',
                duration, notes, device_ip
            ]
            
            self.worksheet.append_row(row_data)
            return True, "Record added successfully"
            
        except Exception as e:
            logger.error(f"Error adding record: {e}")
            return False, f"Error adding record: {e}"
    
    def get_user_records_today(self, email: str) -> List[Dict]:
        """Get user's records for today"""
        if not self.worksheet:
            logger.warning("Google Sheets not available for retrieving records")
            return []
        
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            all_records = self.worksheet.get_all_records()
            
            user_records_today = [
                record for record in all_records
                if record.get('Email') == email and record.get('Date') == today
            ]
            
            return user_records_today
            
        except Exception as e:
            logger.error(f"Error getting user records: {e}")
            return []
    
    def get_user_status(self, email: str) -> str:
        """Check if user is currently checked in"""
        records = self.get_user_records_today(email)
        
        if not records:
            return 'not-checked-in'
        
        # Check latest record
        latest_record = records[-1]
        if latest_record.get('Action') == 'check-in':
            return 'checked-in'
        else:
            return 'checked-out'
    
    def get_all_records(self, limit: int = 100) -> List[Dict]:
        """Get all attendance records"""
        if not self.worksheet:
            logger.warning("Google Sheets not available for retrieving records")
            return []
        
        try:
            records = self.worksheet.get_all_records()
            # Return latest records first
            return records[-limit:][::-1]
        except Exception as e:
            logger.error(f"Error getting all records: {e}")
            return []
    
    def get_daily_summary(self, date: str = None) -> Dict:
        """Get daily attendance summary"""
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        if not self.worksheet:
            return {}
        
        try:
            all_records = self.worksheet.get_all_records()
            daily_records = [
                record for record in all_records
                if record.get('Date') == date
            ]
            
            summary = {
                'date': date,
                'total_check_ins': sum(1 for r in daily_records if r.get('Action') == 'check-in'),
                'total_check_outs': sum(1 for r in daily_records if r.get('Action') == 'check-out'),
                'unique_users': len(set(r.get('Email') for r in daily_records if r.get('Email'))),
                'records': daily_records
            }
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting daily summary: {e}")
            return {}

# Initialize the tracker
tracker = AttendanceTracker()

@app.route('/')
def index():
    """Main attendance tracking page"""
    return render_template('index.html')

@app.route('/admin')
def admin():
    """Admin dashboard"""
    records = tracker.get_all_records(50)
    today_summary = tracker.get_daily_summary()
    return render_template('admin.html', records=records, summary=today_summary)

@app.route('/scoreboard')
def scoreboard():
    """Display attendance leaderboard with total hours"""
    # Get all records
    all_records = tracker.get_all_records(limit=1000)  # Get more records for accurate stats
    
    # Calculate hours for each person
    person_stats = {}
    
    for record in all_records:
        name = record.get('Name', '')
        action = record.get('Action', '')
        date = record.get('Date', '')
        time = record.get('Time', '')
        
        if not name:
            continue
            
        if name not in person_stats:
            person_stats[name] = {
                'name': name,
                'total_hours': 0.0,
                'sessions': [],
                'current_checkin': None,
                'last_activity': ''
            }
        
        # Parse datetime for calculations
        try:
            from datetime import datetime
            record_datetime = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
        except:
            continue
            
        if action == 'check-in':
            person_stats[name]['current_checkin'] = record_datetime
        elif action == 'check-out' and person_stats[name]['current_checkin']:
            # Calculate session duration
            checkin_time = person_stats[name]['current_checkin']
            duration_seconds = (record_datetime - checkin_time).total_seconds()
            duration_hours = duration_seconds / 3600  # Convert to hours
            
            if duration_hours > 0 and duration_hours < 24:  # Sanity check
                person_stats[name]['total_hours'] += duration_hours
                person_stats[name]['sessions'].append({
                    'date': date,
                    'duration': duration_hours
                })
            
            person_stats[name]['current_checkin'] = None
            
        # Update last activity
        if date and date > person_stats[name]['last_activity']:
            person_stats[name]['last_activity'] = date
    
    # Create ranking list
    ranking_list = []
    for name, stats in person_stats.items():
        if stats['total_hours'] > 0:  # Only include people with recorded hours
            ranking_list.append({
                'rank': 0,  # Will be set after sorting
                'name': name,
                'total_hours': round(stats['total_hours'], 1),
                'sessions_count': len(stats['sessions']),
                'avg_session_hours': round(stats['total_hours'] / len(stats['sessions']), 1) if stats['sessions'] else 0,
                'last_activity': stats['last_activity'] or 'Never'
            })
    
    # Sort by total hours (highest first)
    ranking_list.sort(key=lambda x: x['total_hours'], reverse=True)
    
    # Assign ranks
    for i, person in enumerate(ranking_list):
        person['rank'] = i + 1
    
    return render_template('scoreboard.html', rankings=ranking_list, total_people=len(ranking_list))

@app.route('/api/check-in', methods=['POST'])
def check_in():
    """Handle check-in requests"""
    data = request.get_json()
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    notes = data.get('notes', '').strip()
    
    if not name or not email:
        return jsonify({'success': False, 'message': 'Name and email are required'})
    
    # Check current status
    current_status = tracker.get_user_status(email)
    if current_status == 'checked-in':
        return jsonify({'success': False, 'message': 'Already checked in today'})
    
    device_ip = request.remote_addr
    success, message = tracker.add_attendance_record(name, email, 'check-in', notes, device_ip)
    
    return jsonify({'success': success, 'message': message})

@app.route('/api/check-out', methods=['POST'])
def check_out():
    """Handle check-out requests"""
    data = request.get_json()
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    notes = data.get('notes', '').strip()
    
    if not name or not email:
        return jsonify({'success': False, 'message': 'Name and email are required'})
    
    # Check current status
    current_status = tracker.get_user_status(email)
    if current_status != 'checked-in':
        return jsonify({'success': False, 'message': 'Not currently checked in'})
    
    device_ip = request.remote_addr
    success, message = tracker.add_attendance_record(name, email, 'check-out', notes, device_ip)
    
    return jsonify({'success': success, 'message': message})

@app.route('/api/status/<email>')
def get_status(email):
    """Get user's current status"""
    status = tracker.get_user_status(email)
    records_today = tracker.get_user_records_today(email)
    
    return jsonify({
        'status': status,
        'records_today': records_today
    })

@app.route('/api/summary')
def get_summary():
    """Get today's attendance summary"""
    summary = tracker.get_daily_summary()
    return jsonify(summary)

@app.route('/api/records')
def get_records():
    """Get attendance records"""
    limit = request.args.get('limit', 50, type=int)
    records = tracker.get_all_records(limit)
    return jsonify(records)

@app.route('/api/names')
def get_preset_names():
    """Get preset names list"""
    return jsonify(PRESET_NAMES)

@app.route('/api/upload-names', methods=['POST'])
def upload_names():
    """Upload CSV file to update names list"""
    global PRESET_NAMES
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})
    
    if not file.filename.lower().endswith('.csv'):
        return jsonify({'success': False, 'message': 'Please upload a CSV file'})
    
    try:
        # Read CSV content
        content = file.read().decode('utf-8')
        lines = content.strip().split('\n')
        
        # Extract names (assume first column is names, skip header if present)
        new_names = []
        for i, line in enumerate(lines):
            if line.strip():
                # Split by comma and take first column
                name = line.split(',')[0].strip().strip('"')
                
                # Skip header row if it looks like a header
                if i == 0 and (name.lower() in ['name', 'names', 'full name', 'employee']):
                    continue
                    
                if name and name not in new_names:
                    new_names.append(name)
        
        if not new_names:
            return jsonify({'success': False, 'message': 'No valid names found in CSV file'})
        
        # Update the global names list
        PRESET_NAMES = new_names
        
        # Save to a file for persistence (optional)
        try:
            with open('names_list.csv', 'w', encoding='utf-8') as f:
                for name in PRESET_NAMES:
                    f.write(f'"{name}"\n')
        except Exception as e:
            logger.warning(f'Could not save names list to file: {e}')
        
        logger.info(f'Updated names list with {len(new_names)} names')
        return jsonify({
            'success': True, 
            'message': f'Successfully uploaded {len(new_names)} names',
            'names': new_names
        })
        
    except Exception as e:
        logger.error(f'Error processing CSV file: {e}')
        return jsonify({'success': False, 'message': f'Error processing file: {str(e)}'})

@app.route('/api/add-name', methods=['POST'])
def add_name():
    """Add a single name to the list"""
    global PRESET_NAMES
    
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})
    
    if name in PRESET_NAMES:
        return jsonify({'success': False, 'message': 'Name already exists'})
    
    PRESET_NAMES.append(name)
    
    # Save to file
    try:
        with open('names_list.csv', 'w', encoding='utf-8') as f:
            for n in PRESET_NAMES:
                f.write(f'"{n}"\n')
    except Exception as e:
        logger.warning(f'Could not save names list to file: {e}')
    
    return jsonify({'success': True, 'message': f'Added {name} to the list', 'names': PRESET_NAMES})

@app.route('/api/remove-name', methods=['POST'])
def remove_name():
    """Remove a name from the list"""
    global PRESET_NAMES
    
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})
    
    if name not in PRESET_NAMES:
        return jsonify({'success': False, 'message': 'Name not found'})
    
    PRESET_NAMES.remove(name)
    
    # Save to file
    try:
        with open('names_list.csv', 'w', encoding='utf-8') as f:
            for n in PRESET_NAMES:
                f.write(f'"{n}"\n')
    except Exception as e:
        logger.warning(f'Could not save names list to file: {e}')
    
    return jsonify({'success': True, 'message': f'Removed {name} from the list', 'names': PRESET_NAMES})

@app.route('/api/toggle-attendance', methods=['POST'])
def toggle_attendance():
    """Toggle attendance for a preset name"""
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})
    
    if name not in PRESET_NAMES:
        return jsonify({'success': False, 'message': 'Name not in preset list'})
    
    # Create unique request key
    request_key = f"toggle_{name}_{request.remote_addr}"
    
    # Check for duplicate requests
    if is_duplicate_request(request_key):
        logger.warning(f"Duplicate toggle request detected for {name}")
        return jsonify({
            'success': False, 
            'message': 'Request already in progress. Please wait.'
        })
    
    try:
        # Use name directly as identifier
        email = name  # Simple identifier
        
        # Check current status
        current_status = tracker.get_user_status(email)
        
        if current_status == 'checked-in':
            # Check out
            action = 'check-out'
        else:
            # Check in
            action = 'check-in'
        
        device_ip = request.remote_addr
        success, message = tracker.add_attendance_record(name, email, action, '', device_ip)
        
        if success:
            logger.info(f"Successfully toggled {name} to {action}")
        else:
            logger.error(f"Failed to toggle {name}: {message}")
        
        return jsonify({
            'success': success, 
            'message': message,
            'action': action,
            'new_status': 'checked-in' if action == 'check-in' else 'checked-out'
        })
        
    except Exception as e:
        logger.error(f"Error in toggle_attendance for {name}: {e}")
        return jsonify({
            'success': False,
            'message': 'Server error occurred. Please try again.'
        })
    finally:
        # Remove from cache after processing (with a small delay)
        import threading
        def cleanup_cache():
            time.sleep(1)  # Wait 1 second before cleanup
            with request_lock:
                request_cache.pop(request_key, None)
        
        threading.Thread(target=cleanup_cache).start()

if __name__ == '__main__':
    # Get host and port from environment or use defaults
    host = os.environ.get('HOST', '0.0.0.0')  # 0.0.0.0 allows access from other devices
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Starting attendance tracker on {host}:{port}")
    app.run(host=host, port=port, debug=debug)