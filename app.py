#!/usr/bin/env python3
"""
Attendance Tracking System
A Flask web application for tracking attendance using local SQLite database with Google Sheets backup.
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, Response
import gspread
from google.oauth2.service_account import Credentials
from typing import Dict, List, Optional
import hashlib
from dotenv import load_dotenv
from functools import wraps
import time
from threading import Lock, Thread
import json
from collections import defaultdict

# Database lock to prevent race conditions between user operations and background sync
db_lock = Lock()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LocalDatabase:
    def __init__(self):
        self.db_path = 'attendance.db'
        self.init_database()
    
    def init_database(self):
        """Initialize the SQLite database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attendance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    duration_hours REAL DEFAULT 0,
                    synced BOOLEAN DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_hours (
                    name TEXT PRIMARY KEY,
                    total_hours REAL DEFAULT 0,
                    last_checkin TEXT,
                    session_count INTEGER DEFAULT 0,
                    last_activity TEXT
                )
            ''')
            
            # Add duration_hours column if it doesn't exist (for existing databases)
            try:
                cursor.execute('ALTER TABLE attendance_records ADD COLUMN duration_hours REAL DEFAULT 0')
                logger.info('Added duration_hours column to attendance_records')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            conn.commit()
            conn.close()
            logger.info("✅ Local database initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize local database: {e}")
    
    def add_record(self, name: str, action: str) -> bool:
        """Add a record to the local database and update hours tracking"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                timestamp = datetime.now().isoformat()
                duration_hours = 0
                
                # Get or create user hours record
                cursor.execute('SELECT total_hours, last_checkin, session_count FROM user_hours WHERE name = ?', (name,))
                user_data = cursor.fetchone()
                
                if not user_data:
                    cursor.execute('INSERT INTO user_hours (name, total_hours, session_count, last_activity) VALUES (?, 0, 0, ?)', (name, timestamp[:10]))
                    total_hours, last_checkin, session_count = 0, None, 0
                else:
                    total_hours, last_checkin, session_count = user_data
                
                if action == 'check-in':
                    # Update last_checkin time
                    cursor.execute('UPDATE user_hours SET last_checkin = ?, last_activity = ? WHERE name = ?', (timestamp, timestamp[:10], name))
                    
                elif action == 'check-out' and last_checkin:
                    # Calculate session duration
                    try:
                        checkin_time = datetime.fromisoformat(last_checkin)
                        checkout_time = datetime.fromisoformat(timestamp)
                        duration_seconds = (checkout_time - checkin_time).total_seconds()
                        duration_hours = duration_seconds / 3600
                        
                        if 0 < duration_hours < 24:  # Sanity check
                            # Update total hours and session count
                            new_total_hours = total_hours + duration_hours
                            new_session_count = session_count + 1
                            cursor.execute('''
                                UPDATE user_hours 
                                SET total_hours = ?, last_checkin = NULL, session_count = ?, last_activity = ?
                                WHERE name = ?
                            ''', (new_total_hours, new_session_count, timestamp[:10], name))
                            
                            logger.info(f"Session complete for {name}: {duration_hours:.2f} hours (Total: {new_total_hours:.2f}h)")
                    except Exception as e:
                        logger.error(f"Error calculating duration for {name}: {e}")
                
                # Insert attendance record
                cursor.execute('''
                    INSERT INTO attendance_records (timestamp, name, action, duration_hours, synced)
                    VALUES (?, ?, ?, ?, 0)
                ''', (timestamp, name, action, duration_hours))
                
                conn.commit()
                conn.close()
                return True
            except Exception as e:
                logger.error(f"Error adding record to local database: {e}")
                return False
    
    def get_user_status(self, name: str) -> str:
        """Get the current status of a user"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT action FROM attendance_records 
                    WHERE name = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                ''', (name,))
                
                result = cursor.fetchone()
                conn.close()
                
                if result:
                    return 'checked-in' if result[0] == 'check-in' else 'checked-out'
                return 'checked-out'
            except Exception as e:
                logger.error(f"Error getting user status: {e}")
                return 'checked-out'
    
    def get_leaderboard_data(self) -> List[Dict]:
        """Get leaderboard data from user_hours table"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT name, total_hours, session_count, last_activity
                    FROM user_hours 
                    WHERE total_hours > 0
                    ORDER BY total_hours DESC
                ''')
                
                results = []
                for i, row in enumerate(cursor.fetchall()):
                    name, total_hours, session_count, last_activity = row
                    avg_hours = total_hours / session_count if session_count > 0 else 0
                    
                    results.append({
                        'rank': i + 1,
                        'name': name,
                        'total_hours': round(total_hours, 2),
                        'sessions_count': session_count,
                        'avg_session_hours': round(avg_hours, 2),
                        'last_activity': last_activity or 'Never'
                    })
                
                conn.close()
                return results
            except Exception as e:
                logger.error(f"Error getting leaderboard data: {e}")
                return []
    
    def get_records(self, limit: int = 100, date_filter: str = None, name_filter: str = None) -> List[Dict]:
        """Get records from the local database"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                query = 'SELECT timestamp, name, action FROM attendance_records'
                params = []
                conditions = []
                
                if date_filter:
                    conditions.append("DATE(timestamp) = ?")
                    params.append(date_filter)
                
                if name_filter:
                    conditions.append("name = ?")
                    params.append(name_filter)
                
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                
                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)
                
                cursor.execute(query, params)
                
                records = []
                for row in cursor.fetchall():
                    timestamp, name, action = row
                    dt = datetime.fromisoformat(timestamp)
                    records.append({
                        'Timestamp': timestamp,
                        'Name': name,
                        'Action': action,
                        'Date': dt.strftime('%Y-%m-%d')
                    })
                
                conn.close()
                return records
            except Exception as e:
                logger.error(f"Error getting records: {e}")
                return []
    
    def get_unsynced_records(self) -> List[Dict]:
        """Get records that haven't been synced to Google Sheets"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT id, timestamp, name, action 
                    FROM attendance_records 
                    WHERE synced = 0 
                    ORDER BY timestamp ASC
                ''')
                
                records = []
                for row in cursor.fetchall():
                    id_val, timestamp, name, action = row
                    records.append({
                        'id': id_val,
                        'Timestamp': timestamp,
                        'Name': name,
                        'Action': action
                    })
                
                conn.close()
                return records
            except Exception as e:
                logger.error(f"Error getting unsynced records: {e}")
                return []
    
    def mark_records_synced(self, record_ids: List[int]):
        """Mark records as synced to Google Sheets"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                for record_id in record_ids:
                    cursor.execute('UPDATE attendance_records SET synced = 1 WHERE id = ?', (record_id,))
                
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Error marking records as synced: {e}")

# Global instances
local_db = LocalDatabase()

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
        """Initialize Google Sheets connection for sync backup"""
        try:
            creds_path = os.environ.get('GOOGLE_CREDENTIALS_PATH', 'credentials.json')
            if not os.path.exists(creds_path):
                logger.warning(f"Credentials file not found: {creds_path}. Google Sheets sync disabled.")
                return
            
            creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
            self.gc = gspread.authorize(creds)
            
            spreadsheet_name = os.environ.get('SPREADSHEET_NAME', 'Test Attendance Tracker')
            self.sheet = self.gc.open(spreadsheet_name)
            
            try:
                self.worksheet = self.sheet.worksheet('Attendance')
                logger.info("Found existing 'Attendance' worksheet")
            except gspread.WorksheetNotFound:
                logger.info("Creating new 'Attendance' worksheet")
                self.worksheet = self.sheet.add_worksheet(title='Attendance', rows=1000, cols=10)
                self._setup_headers()
            
            logger.info("✅ Google Sheets connection established successfully")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Sheets: {e}")
            self.worksheet = None
    
    def _setup_headers(self):
        """Set up headers in the Google Sheet"""
        if not self.worksheet:
            return
        
        try:
            headers = [
                'Timestamp', 'Name', 'Action', 'Date',
                'Check-in Time', 'Check-out Time', 'Duration (hrs)', 'Total Hours', 'Notes'
            ]
            self.worksheet.append_row(headers)
            logger.info("✅ Headers added to Google Sheet")
        except Exception as e:
            logger.error(f"❌ Failed to setup headers: {e}")
    
    def add_attendance_record(self, name: str, action: str, notes: str = '', device_ip: str = ''):
        """Add an attendance record - uses local database first, syncs to sheets later"""
        try:
            success = local_db.add_record(name, action)
            if not success:
                return False, "Failed to add record to local database"
            
            logger.info(f"✅ Added {name} - {action} to local database")
            return True, "Record added successfully"
            
        except Exception as e:
            logger.error(f"Error adding record: {e}")
            return False, f"Error adding record: {e}"
    

    
    def get_user_status(self, name: str) -> str:
        """Check if user is currently checked in - now uses local database"""
        return local_db.get_user_status(name)
    
    def sync_to_google_sheets(self):
        """Sync unsynced records from local database to Google Sheets and update leaderboard"""
        if not self.worksheet:
            logger.warning("Google Sheets not available for syncing")
            return
        
        try:
            logger.info(f"🔗 Syncing to spreadsheet: '{self.sheet.title}' -> worksheet: '{self.worksheet.title}'")
            
            # Test write permissions by getting current row count
            try:
                current_values = self.worksheet.get_all_values()
                logger.info(f"📊 Current worksheet has {len(current_values)} rows")
                if len(current_values) == 0:
                    logger.warning("⚠️ Worksheet appears empty, setting up headers...")
                    self._setup_headers()
            except Exception as perm_error:
                logger.error(f"❌ Cannot read worksheet (permission issue?): {perm_error}")
                return
            
            unsynced_records = local_db.get_unsynced_records()
            if not unsynced_records:
                logger.info("No records to sync")
                return
            
            logger.info(f"Syncing {len(unsynced_records)} records to Google Sheets...")
            
            # Process in smaller batches to avoid rate limits
            batch_size = 5  # Process 5 records at a time
            synced_ids = []
            
            for i in range(0, len(unsynced_records), batch_size):
                batch = unsynced_records[i:i+batch_size]
                logger.info(f"Processing batch {i//batch_size + 1} of {(len(unsynced_records) + batch_size - 1)//batch_size}")
                
                for record in batch:
                    try:
                        now = datetime.fromisoformat(record['Timestamp'])
                        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
                        date = now.strftime('%Y-%m-%d')
                        time_str = now.strftime('%H:%M:%S')
                        
                        # Get duration and total hours for this record
                        duration_hours = ''
                        total_hours = ''
                        if record['Action'] == 'check-out':
                            # Get the duration for this record from the attendance_records table
                            try:
                                with db_lock:
                                    conn = sqlite3.connect(local_db.db_path)
                                    cursor = conn.cursor()
                                    # Get duration from this record
                                    cursor.execute('SELECT duration_hours FROM attendance_records WHERE id = ?', (record['id'],))
                                    result = cursor.fetchone()
                                    if result and result[0] > 0:
                                        duration_hours = f'{result[0]:.2f}'
                                    
                                    # Get current total hours for this user
                                    cursor.execute('SELECT total_hours FROM user_hours WHERE name = ?', (record['Name'],))
                                    total_result = cursor.fetchone()
                                    if total_result:
                                        total_hours = f'{total_result[0]:.2f}'
                                    
                                    conn.close()
                            except Exception as e:
                                logger.error(f"Error getting hours for record {record['id']}: {e}")
                        
                        row_data = [
                            timestamp, record['Name'], record['Action'], date,
                            time_str if record['Action'] == 'check-in' else '',
                            time_str if record['Action'] == 'check-out' else '',
                            duration_hours, total_hours, ''  # duration, total_hours, notes
                        ]
                        
                        logger.info(f"📝 Writing row: {record['Name']} - {record['Action']} - {timestamp}")
                        try:
                            self.worksheet.append_row(row_data)
                            synced_ids.append(record['id'])
                            logger.info(f"✅ Successfully wrote record {record['id']}")
                        except Exception as append_error:
                            logger.error(f"❌ Failed to append row for record {record['id']}: {append_error}")
                            raise append_error
                        
                        # Add delay between individual requests to avoid rate limits
                        import time
                        time.sleep(0.5)  # 500ms delay between each write
                        
                    except Exception as e:
                        logger.error(f"Failed to sync record {record['id']}: {e}")
                        break  # Stop on first failure to maintain order
                
                # Longer delay between batches
                if i + batch_size < len(unsynced_records):
                    import time
                    time.sleep(2.0)  # 2 second delay between batches
            
            if synced_ids:
                local_db.mark_records_synced(synced_ids)
                logger.info(f"✅ Successfully synced {len(synced_ids)} records to Google Sheets")
                
        except Exception as e:
            logger.error(f"Error during sync to Google Sheets: {e}")
    
    def get_all_records(self, limit: int = 100) -> List[Dict]:
        """Get all attendance records - now uses local database"""
        return local_db.get_records(limit=limit)
    
    def get_daily_summary(self, date: str = None) -> Dict:
        """Get daily attendance summary from local database"""
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        try:
            daily_records = local_db.get_records(date_filter=date)
            
            summary = {
                'date': date,
                'total_check_ins': sum(1 for r in daily_records if r.get('Action') == 'check-in'),
                'total_check_outs': sum(1 for r in daily_records if r.get('Action') == 'check-out'),
                'unique_users': len(set(r.get('Name') for r in daily_records if r.get('Name'))),
                'records': daily_records
            }
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting daily summary: {e}")
            return {}
            logger.error(f"Error getting daily summary: {e}")
            return {}

def background_sync():
    """Background thread function to periodically sync to Google Sheets"""
    while True:
        try:
            time.sleep(60)  # Wait 1 minute between sync attempts
            tracker.sync_to_google_sheets()
        except Exception as e:
            logger.error(f"Error in background sync: {e}")
            time.sleep(300)  # Wait 5 minutes before retrying on error

# Initialize the tracker
tracker = AttendanceTracker()

# Start background sync thread
sync_thread = Thread(target=background_sync, daemon=True)
sync_thread.start()
logger.info("🔄 Started background sync thread")

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

@app.route('/sync-test')
def sync_test():
    """Real-time sync test page"""
    return render_template('sync-test.html')

@app.route('/test.html')
def test_page():
    """Test page for debugging JavaScript"""
    from flask import send_from_directory
    return send_from_directory('.', 'test.html')

@app.route('/leaderboard')
def leaderboard():
    """Display attendance leaderboard with total hours"""
    try:
        # Get leaderboard data from the user_hours table
        rankings = local_db.get_leaderboard_data()
        
        if not rankings:
            return render_template('scoreboard.html', rankings=[], total_people=0, total_team_hours=0, error="No attendance data found.")
        
        # Calculate total team hours
        total_team_hours = sum(person['total_hours'] for person in rankings)
        
        return render_template('scoreboard.html', rankings=rankings, total_people=len(rankings), total_team_hours=total_team_hours)
    
    except Exception as e:
        logger.error(f"Error in leaderboard: {e}")
        return render_template('scoreboard.html', rankings=[], total_people=0, error=f"Could not load leaderboard data: {e}")

@app.route('/api/check-in', methods=['POST'])
def check_in():
    """Handle check-in requests"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        notes = data.get('notes', '').strip()
        
        if not name:
            return jsonify({'success': False, 'message': 'Name is required'})
        
        # Check current status
        current_status = tracker.get_user_status(name)
        if current_status == 'check-in':
            return jsonify({'success': False, 'message': 'Already checked in today'})
        
        device_ip = request.remote_addr
        success, message = tracker.add_attendance_record(name, 'check-in', notes, device_ip)
        
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        logger.error(f"Error in check-in endpoint: {e}")
        return jsonify({'success': False, 'message': 'Server error occurred. Please try again.'})

@app.route('/api/check-out', methods=['POST'])
def check_out():
    """Handle check-out requests"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        notes = data.get('notes', '').strip()
        
        if not name:
            return jsonify({'success': False, 'message': 'Name is required'})
        
        # Check current status
        current_status = tracker.get_user_status(name)
        if current_status != 'check-in':
            return jsonify({'success': False, 'message': 'Not currently checked in'})
        
        device_ip = request.remote_addr
        success, message = tracker.add_attendance_record(name, 'check-out', notes, device_ip)
        
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        logger.error(f"Error in check-out endpoint: {e}")
        return jsonify({'success': False, 'message': 'Server error occurred. Please try again.'})
    
    return jsonify({'success': success, 'message': message})

@app.route('/api/status/<name>')
def get_status(name):
    """Get user's current status"""
    try:
        status = tracker.get_user_status(name)
        today = datetime.now().strftime('%Y-%m-%d')
        records_today = local_db.get_records(date_filter=today, name_filter=name)
        
        return jsonify({
            'status': status,
            'records_today': records_today
        })
    except Exception as e:
        logger.error(f"Error getting status for {name}: {e}")
        return jsonify({
            'status': 'error',
            'records_today': [],
            'message': 'Could not retrieve status'
        })

@app.route('/api/summary')
def get_summary():
    """Get today's attendance summary"""
    try:
        summary = tracker.get_daily_summary()
        return jsonify(summary)
    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return jsonify({'error': 'Could not retrieve summary'})

@app.route('/api/records')
def get_records():
    """Get attendance records"""
    try:
        limit = request.args.get('limit', 50, type=int)
        records = tracker.get_all_records(limit)
        return jsonify(records)
    except Exception as e:
        logger.error(f"Error getting records: {e}")
        return jsonify({'error': 'Could not retrieve records'})

@app.route('/api/cache/clear', methods=['POST'])
@app.route('/health')
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'google_sheets': 'connected' if tracker.worksheet else 'disconnected'
    })

@app.route('/api/quick-status')
def quick_status():
    """Quick status that shows preset names without Google Sheets calls"""
    try:
        # Just return preset names without any Google Sheets calls
        users = []
        for name in PRESET_NAMES:
            users.append({
                'name': name,
                'email': name,
                'status': 'checked-out',
                'last_action': None,
                'last_timestamp': None
            })
        
        return jsonify({
            'status': 'ready',
            'users': users,
            'summary': {
                'total_users': len(users),
                'checked_in': 0,
                'checked_out': 0,
                'not_checked_in': len(users)
            },
            'last_updated': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in quick status: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/global-status')
def global_status():
    """Get current status of all users - now uses local database"""
    start_time = time.time()
    try:
        logger.info("Global status request started")
        
        # Get today's records from local database
        today = datetime.now().strftime('%Y-%m-%d')
        logger.info(f"Getting records for date: {today}")
        
        records_start = time.time()
        all_records = local_db.get_records(date_filter=today)
        logger.info(f"Records fetched in {time.time() - records_start:.3f}s, count: {len(all_records)}")
        
        # Calculate current status for each user
        process_start = time.time()
        user_status = {}
        
        for record in all_records:
            name = record.get('Name')
            action = record.get('Action')
            timestamp = record.get('Timestamp')
            
            if not name:
                continue
            
            if name not in user_status:
                user_status[name] = {
                    'name': name,
                    'email': name,  # Use name as identifier
                    'status': 'checked-out',
                    'last_action': None,
                    'last_timestamp': None
                }
            
            # Update with latest action
            if not user_status[name]['last_timestamp'] or timestamp > user_status[name]['last_timestamp']:
                user_status[name]['last_action'] = action
                user_status[name]['last_timestamp'] = timestamp
                user_status[name]['status'] = 'checked-in' if action == 'check-in' else 'checked-out'
        
        # Convert to list and add preset names that haven't checked in
        status_list = list(user_status.values())
        
        # Add preset names that haven't appeared in records today
        existing_names = {user['name'] for user in status_list}
        for preset_name in PRESET_NAMES:
            if preset_name not in existing_names:
                status_list.append({
                    'name': preset_name,
                    'email': preset_name,  # Use name as identifier
                    'status': 'checked-out',
                    'last_action': None,
                    'last_timestamp': None
                })
        
        # Sort by name
        status_list.sort(key=lambda x: x['name'])
        
        logger.info(f"Processing completed in {time.time() - process_start:.3f}s")
        logger.info(f"Total global status request time: {time.time() - start_time:.3f}s")
        
        return jsonify({
            'users': status_list,
            'summary': {
                'total_users': len(status_list),
                'checked_in': sum(1 for u in status_list if u['status'] == 'checked-in'),
                'checked_out': sum(1 for u in status_list if u['status'] == 'checked-out')
            },
            'last_updated': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting global status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/status-stream/<name>')
def status_stream(name):
    """Server-Sent Events stream for real-time status updates"""
    logger.info(f"SSE connection established for {name}")
    
    def event_stream():
        client_queue = queue.Queue(maxsize=10)
        status_broadcaster.add_client(name, client_queue)
        logger.info(f"Added SSE client for {name}")
        
        try:
            # Send initial status
            current_status = tracker.get_user_status(name)
            today = datetime.now().strftime('%Y-%m-%d')
            records_today = local_db.get_records(date_filter=today, name_filter=name)
            
            initial_data = {
                'type': 'initial_status',
                'status': current_status,
                'records_today': records_today,
                'name': name
            }
            yield f"data: {json.dumps(initial_data)}\n\n"
            logger.debug(f"Sent initial status for {name}: {current_status}")
            
            # Keep connection alive and send updates
            while True:
                try:
                    # Wait for status updates with timeout
                    data = client_queue.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                    logger.debug(f"Sent update to {name}: {data.get('type')}")
                except queue.Empty:
                    # Send heartbeat to keep connection alive
                    yield "data: {\"type\": \"heartbeat\"}\n\n"
                    logger.debug(f"Sent heartbeat to {name}")
                except Exception as e:
                    logger.error(f"Error in SSE stream for {name}: {e}")
                    break
                    
        except GeneratorExit:
            # Client disconnected
            logger.info(f"SSE client disconnected for {name}")
        except Exception as e:
            logger.error(f"SSE stream error for {name}: {e}")
        finally:
            status_broadcaster.remove_client(name, client_queue)
            logger.info(f"Removed SSE client for {name}")
    
    return Response(event_stream(), mimetype='text/event-stream',
                   headers={'Cache-Control': 'no-cache',
                           'Connection': 'keep-alive',
                           'Access-Control-Allow-Origin': '*'})



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
    
    try:
        # Get current status
        current_status = local_db.get_user_status(name)
        
        # Determine new action
        if current_status == 'checked-in':
            action = 'check-out'
        else:
            action = 'check-in'
        
        # Use the proper add_record method which handles both attendance_records and user_hours
        success = local_db.add_record(name, action)
        
        if success:
            logger.info(f"Successfully toggled {name} to {action}")
            
            return jsonify({
                'success': True,
                'message': f'Successfully {action.replace("-", " ")}',
                'action': action,
                'new_status': 'checked-in' if action == 'check-in' else 'checked-out'
            })
        else:
            logger.error(f"Failed to add record for {name}: {action}")
            return jsonify({
                'success': False,
                'message': 'Database error occurred. Please try again.'
            })
            
    except Exception as e:
        logger.error(f"Error in toggle_attendance for {name}: {e}")
        return jsonify({
            'success': False,
            'message': 'Server error occurred. Please try again.'
        })

@app.route('/api/sign-out-all', methods=['POST'])
def sign_out_all():
    """Sign out all currently checked-in users"""
    try:
        # Get all currently checked-in users
        checked_in_users = []
        
        with db_lock:
            conn = sqlite3.connect(local_db.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Get latest record for each user
            cursor.execute("""
                WITH latest_records AS (
                    SELECT name, action, timestamp, 
                           ROW_NUMBER() OVER (PARTITION BY name ORDER BY timestamp DESC) as rn
                    FROM attendance_records 
                    WHERE date(timestamp) = date('now')
                )
                SELECT name FROM latest_records 
                WHERE rn = 1 AND action = 'check-in'
            """)
            
            checked_in_users = [row['name'] for row in cursor.fetchall()]
            conn.close()
        
        # Check out all users
        checked_out_count = 0
        device_ip = request.remote_addr
        
        for name in checked_in_users:
            success, message = tracker.add_attendance_record(name, 'check-out', 'Mass sign-out by admin', device_ip)
            if success:
                checked_out_count += 1
                logger.info(f"Admin signed out {name}")
            else:
                logger.error(f"Failed to sign out {name}: {message}")
        
        return jsonify({
            'success': True,
            'message': f'Successfully signed out {checked_out_count} users',
            'count': checked_out_count,
            'users': checked_in_users
        })
        
    except Exception as e:
        logger.error(f"Error in sign_out_all: {e}")
        return jsonify({
            'success': False,
            'message': 'Server error occurred. Please try again.'
        })

@app.route('/api/manual-sync', methods=['POST'])
def api_manual_sync():
    """Manually trigger Google Sheets sync for testing"""
    try:
        logger.info("🔧 Manual sync triggered")
        tracker.sync_to_google_sheets()
        return jsonify({'success': True, 'message': 'Manual sync completed'})
    except Exception as e:
        logger.error(f"Error during manual sync: {e}")
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    # Get host and port from environment or use defaults
    host = os.environ.get('HOST', '0.0.0.0')  # 0.0.0.0 allows access from other devices
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Starting attendance tracker on {host}:{port}")
    app.run(host=host, port=port, debug=debug)