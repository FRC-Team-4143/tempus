#!/usr/bin/env python3
"""
Route handlers for Attendance Tracking System
"""

import os
import json
import logging
import sqlite3
from datetime import datetime
from flask import render_template, request, jsonify, redirect, url_for, flash, Response
import gspread
from google.oauth2.service_account import Credentials
from typing import Dict, List, Optional
import time
from threading import Lock, Thread

from database import LocalDatabase, db_lock
from utils import PRESET_NAMES, get_team_roster_mapping, load_names_from_file

logger = logging.getLogger(__name__)

# Global instances
local_db = LocalDatabase()

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', os.path.join(config_dir, 'credentials.json'))
SPREADSHEET_NAME = os.environ.get('SPREADSHEET_NAME', 'Test Attendance Tracker')

# Initialize Google Sheets client
gc = None
try:
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        gc = gspread.authorize(creds)
        logger.debug("✅ Google Sheets connection established successfully")
except Exception as e:
    logger.error(f"❌ Error connecting to Google Sheets: {e}")

class AttendanceTracker:
    def __init__(self):
        self.spreadsheet_name = SPREADSHEET_NAME
        self.worksheet_name = 'Attendance'
        self.gc = gc

    def add_attendance_record(self, name: str, action: str, notes: str = '', device_ip: str = '') -> tuple[bool, str]:
        """Add an attendance record and sync to Google Sheets"""
        try:
            # Add to local database
            success = local_db.add_record(name, action)
            if not success:
                return False, "Failed to add record to local database"

            # Try to sync to Google Sheets
            try:
                self.sync_to_google_sheets()
            except Exception as e:
                logger.warning(f"Failed to sync to Google Sheets: {e}")

            return True, f"Successfully recorded {action} for {name}"

        except Exception as e:
            logger.error(f"Error adding attendance record: {e}")
            return False, str(e)

    def sync_to_google_sheets(self):
        """Sync unsynced records to Google Sheets"""
        if not self.gc:
            logger.warning("Google Sheets not available, skipping sync")
            return

        try:
            with db_lock:
                # Get unsynced records
                conn = sqlite3.connect(local_db.db_path)
                cursor = conn.cursor()
                cursor.execute('SELECT id, timestamp, name, action, duration_hours FROM attendance_records WHERE synced = 0 ORDER BY timestamp')
                unsynced_records = cursor.fetchall()
                conn.close()

                if not unsynced_records:
                    return

                # Open spreadsheet
                spreadsheet = self.gc.open(self.spreadsheet_name)
                worksheet = spreadsheet.worksheet(self.worksheet_name)

                # Get current row count
                current_rows = len(worksheet.get_all_values())

                # Prepare data for batch update
                batch_data = []
                synced_ids = []

                for record in unsynced_records:
                    record_id, timestamp, name, action, duration_hours = record

                    # Convert timestamp to readable format
                    try:
                        dt = datetime.fromisoformat(timestamp)
                        readable_time = dt.strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        readable_time = timestamp

                    # Format duration
                    duration_str = f"{duration_hours:.2f}h" if duration_hours > 0 else ""

                    row_data = [readable_time, name, action, duration_str]
                    batch_data.append(row_data)
                    synced_ids.append(record_id)

                if batch_data:
                    # Add rows to sheet
                    start_row = current_rows + 1
                    worksheet.append_rows(batch_data)

                    # Mark records as synced
                    local_db.mark_records_synced(synced_ids)

        except Exception as e:
            logger.error(f"❌ Error syncing to Google Sheets: {e}")
            raise

# Global tracker instance
tracker = AttendanceTracker()

def background_sync():
    """Background thread to periodically sync to Google Sheets"""
    while True:
        try:
            time.sleep(300)  # Sync every 5 minutes
            tracker.sync_to_google_sheets()
        except Exception as e:
            logger.error(f"Background sync error: {e}")
            time.sleep(60)  # Wait a minute before retrying

# Start background sync thread
sync_thread = Thread(target=background_sync, daemon=True)
sync_thread.start()

# Flask route handlers
def index():
    """Main attendance page"""
    return render_template('index.html')

def admin():
    """Admin dashboard"""
    records = local_db.get_records()
    today_summary = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'total_check_ins': len([r for r in records if r.get('Action') == 'check-in']),
        'total_check_outs': len([r for r in records if r.get('Action') == 'check-out']),
        'unique_users': len(set(r.get('Name') for r in records if r.get('Name'))),
        'records': records
    }
    return render_template('admin.html', records=records, summary=today_summary)

def leaderboard():
    """Leaderboard page"""
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
        return render_template('scoreboard.html', rankings=[], total_people=0, total_team_hours=0, error=f"Could not load leaderboard data: {e}")

def check_in():
    """Manual check-in endpoint"""
    data = request.get_json()
    name = data.get('name', '').strip()
    notes = data.get('notes', '')

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    success, message = tracker.add_attendance_record(name, 'check-in', notes, request.remote_addr)
    return jsonify({'success': success, 'message': message})

def check_out():
    """Manual check-out endpoint"""
    data = request.get_json()
    name = data.get('name', '').strip()
    notes = data.get('notes', '')

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    success, message = tracker.add_attendance_record(name, 'check-out', notes, request.remote_addr)
    return jsonify({'success': success, 'message': message})

def get_status(name):
    """Get status for a specific user"""
    try:
        # Get today's records for this user
        records = local_db.get_records(date_filter=datetime.now().strftime('%Y-%m-%d'), name_filter=name)

        if records:
            latest_record = records[0]  # Records are ordered by timestamp DESC
            status = 'checked-in' if latest_record['Action'] == 'check-in' else 'checked-out'
        else:
            status = 'checked-out'

        return jsonify({
            'success': True,
            'status': status,
            'records_today': records
        })

    except Exception as e:
        logger.error(f"Error getting status for {name}: {e}")
        return jsonify({'success': False, 'error': str(e)})

def get_summary():
    """Get attendance summary"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        records = local_db.get_records(date_filter=today)

        summary = {
            'total_records': len(records),
            'checked_in': len([r for r in records if r['Action'] == 'check-in']),
            'checked_out': len([r for r in records if r['Action'] == 'check-out'])
        }

        return jsonify({'success': True, 'summary': summary})

    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return jsonify({'success': False, 'error': str(e)})

def get_records():
    """Get all attendance records"""
    try:
        date_filter = request.args.get('date')
        name_filter = request.args.get('name')

        records = local_db.get_records(date_filter=date_filter, name_filter=name_filter)
        return jsonify({'success': True, 'records': records})

    except Exception as e:
        logger.error(f"Error getting records: {e}")
        return jsonify({'success': False, 'error': str(e)})

def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

def quick_status():
    """Quick status overview"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        records = local_db.get_records(date_filter=today)

        # Count current check-ins (check-ins minus check-outs)
        user_status = {}
        for record in records:
            name = record['Name']
            action = record['Action']

            if name not in user_status:
                user_status[name] = 0

            if action == 'check-in':
                user_status[name] += 1
            elif action == 'check-out':
                user_status[name] -= 1

        checked_in_count = sum(1 for count in user_status.values() if count > 0)

        return jsonify({
            'success': True,
            'checked_in': checked_in_count,
            'total_users': len(PRESET_NAMES),
            'last_update': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error getting quick status: {e}")
        return jsonify({'success': False, 'error': str(e)})

def global_status():
    """Get current status of all users grouped by team - now uses local database"""
    try:
        # Get team roster mapping
        team_mapping = get_team_roster_mapping()

        # Get today's records from local database
        today = datetime.now().strftime('%Y-%m-%d')

        all_records = local_db.get_records(date_filter=today)

        # Calculate current status for each user
        user_status = {}

        for record in all_records:
            name = record.get('Name')
            action = record.get('Action')
            timestamp = record.get('Timestamp')

            if not name or name.startswith('--- Team'):  # Skip team headers
                continue

            if name not in user_status:
                team_number = team_mapping.get(name, '4143')  # Default to 4143
                user_status[name] = {
                    'name': name,
                    'email': name,  # Use name as identifier
                    'status': 'checked-out',
                    'last_action': None,
                    'last_timestamp': None,
                    'team': team_number
                }

            # Update with latest action
            if not user_status[name]['last_timestamp'] or timestamp > user_status[name]['last_timestamp']:
                user_status[name]['last_action'] = action
                user_status[name]['last_timestamp'] = timestamp
                user_status[name]['status'] = 'checked-in' if action == 'check-in' else 'checked-out'

        # Add preset names that haven't appeared in records today
        existing_names = {user['name'] for user in user_status.values()}
        for preset_name in PRESET_NAMES:
            if preset_name not in existing_names and not preset_name.startswith('--- Team'):
                team_number = team_mapping.get(preset_name, '4143')  # Default to 4143
                user_status[preset_name] = {
                    'name': preset_name,
                    'email': preset_name,  # Use name as identifier
                    'status': 'checked-out',
                    'last_action': None,
                    'last_timestamp': None,
                    'team': team_number
                }

        # Group by team
        teams = {'4143': [], '4423': []}
        summary = {'team_4143_checked_in': 0, 'team_4143_total': 0, 'team_4423_checked_in': 0, 'team_4423_total': 0}

        for user in user_status.values():
            team = user['team']
            if team in teams:
                teams[team].append(user)
                summary[f'team_{team}_total'] += 1
                if user['status'] == 'checked-in':
                    summary[f'team_{team}_checked_in'] += 1

        # Sort users within teams
        for team_users in teams.values():
            team_users.sort(key=lambda x: x['name'])

        return jsonify({
            'success': True,
            'teams': teams,
            'summary': summary,
            'last_updated': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error in global_status: {e}")
        return jsonify({'success': False, 'error': str(e)})

def status_stream(name):
    """Server-sent events for real-time status updates"""
    def generate():
        # Send initial status
        status_data = get_status(name).get_json()
        yield f"data: {json.dumps(status_data)}\n\n"

        # Keep connection alive (simplified - in production you'd want proper SSE)
        import time
        time.sleep(30)  # Keep alive for 30 seconds
        yield "data: {\"type\": \"heartbeat\"}\n\n"

    return Response(generate(), mimetype='text/event-stream')

def get_preset_names():
    """Get the list of preset names"""
    # Convert PRESET_NAMES to format with team information
    team_mapping = get_team_roster_mapping()

    names_with_teams = []
    for name in PRESET_NAMES:
        if name.startswith('--- Team'):
            names_with_teams.append({'name': name, 'team': 'header'})
        else:
            team = team_mapping.get(name, 'unknown')
            names_with_teams.append({'name': name, 'team': team})

    return jsonify({'success': True, 'names': names_with_teams})

def upload_names():
    """Upload names from CSV"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file provided'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})

    if not file.filename.endswith('.csv'):
        return jsonify({'success': False, 'message': 'File must be CSV'})

    try:
        # Save uploaded file
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        file.save(os.path.join(data_dir, 'team_roster.csv'))

        # Reload names
        load_names_from_file()

        return jsonify({'success': True, 'message': 'Names uploaded successfully'})

    except Exception as e:
        logger.error(f"Error uploading names: {e}")
        return jsonify({'success': False, 'message': 'Error uploading file'})

def add_name():
    """Add a name to the preset list"""
    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    if name in PRESET_NAMES:
        return jsonify({'success': False, 'message': 'Name already exists'})

    PRESET_NAMES.append(name)

    # Save to file (simplified - in production you'd want better persistence)
    try:
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        with open(os.path.join(data_dir, 'names_list.csv'), 'a') as f:
            f.write(f'"{name}"\n')
    except Exception as e:
        logger.warning(f"Could not save name to file: {e}")

    return jsonify({'success': True, 'message': f'Added {name} to the list', 'names': PRESET_NAMES})

def remove_name():
    """Remove a name from the preset list"""
    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    if name not in PRESET_NAMES:
        return jsonify({'success': False, 'message': 'Name not found'})

    PRESET_NAMES.remove(name)

    # Update file (simplified)
    try:
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        with open(os.path.join(data_dir, 'names_list.csv'), 'w') as f:
            for n in PRESET_NAMES:
                f.write(f'"{n}"\n')
    except Exception as e:
        logger.warning(f"Could not update names file: {e}")

    return jsonify({'success': True, 'message': f'Removed {name} from the list', 'names': PRESET_NAMES})

def toggle_attendance():
    """Toggle attendance for a preset name"""
    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})

    # Skip team header entries
    if name.startswith('--- Team'):
        return jsonify({'success': False, 'message': 'Cannot check in team headers'})

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

def sign_out_all():
    """Sign out all currently checked-in users"""
    try:
        # Get team roster mapping
        team_mapping = get_team_roster_mapping()

        # Get today's records to find checked-in users
        today = datetime.now().strftime('%Y-%m-%d')
        all_records = local_db.get_records(date_filter=today)

        # Calculate current status for each user
        user_status = {}
        for record in all_records:
            name = record.get('Name')
            action = record.get('Action')
            timestamp = record.get('Timestamp')

            if not name or name.startswith('--- Team'):
                continue

            if name not in user_status:
                user_status[name] = {'last_action': None, 'last_timestamp': None}

            # Update with latest action
            if not user_status[name]['last_timestamp'] or timestamp > user_status[name]['last_timestamp']:
                user_status[name]['last_action'] = action
                user_status[name]['last_timestamp'] = timestamp

        # Find users who are currently checked in
        checked_in_users = [name for name, status in user_status.items() if status['last_action'] == 'check-in']

        if not checked_in_users:
            return jsonify({
                'success': True,
                'message': 'No users currently checked in',
                'count': 0,
                'users': []
            })

        # Sign out each checked-in user
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

def api_manual_sync():
    """Manually trigger Google Sheets sync for testing"""
    try:
        logger.info("🔧 Manual sync triggered")
        tracker.sync_to_google_sheets()
        return jsonify({'success': True, 'message': 'Manual sync completed'})
    except Exception as e:
        logger.error(f"Error during manual sync: {e}")
        return jsonify({'success': False, 'error': str(e)})

def api_user_hours_summary():
    """Get summary of all users and their total hours"""
    try:
        with db_lock:
            db = LocalDatabase()
            conn = sqlite3.connect(db.db_path)
            cursor = conn.cursor()

            # Get total hours for each user from user_hours table (includes manual adjustments)
            cursor.execute('''
                SELECT uh.name, uh.total_hours, uh.session_count, uh.last_activity
                FROM user_hours uh
                ORDER BY uh.total_hours DESC
            ''')

            user_hours = cursor.fetchall()
            conn.close()

            # Format results
            summary = []
            for row in user_hours:
                summary.append({
                    'name': row[0],
                    'total_hours': round(row[1], 2),
                    'sessions': row[2] or 0,
                    'last_activity': row[3] or 'Never'
                })

            return jsonify({'success': True, 'users': summary})

    except Exception as e:
        logger.error(f"Error getting user hours summary: {e}")
        return jsonify({'success': False, 'error': str(e)})

def api_adjust_user_hours():
    """Adjust a user's total hours (admin function)"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        adjustment_type = data.get('adjustment_type', 'add')
        adjustment_hours = float(data.get('hours', 0))

        if not name:
            return jsonify({'success': False, 'message': 'Name is required'})

        # Get current hours
        db = LocalDatabase()
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT total_hours FROM user_hours WHERE name = ?', (name,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'success': False, 'message': f'User {name} not found'})

        current_hours = result[0]

        # Calculate new hours based on adjustment type
        if adjustment_type == 'add':
            new_hours = current_hours + adjustment_hours
        elif adjustment_type == 'subtract':
            new_hours = max(0, current_hours - adjustment_hours)
        elif adjustment_type == 'set':
            new_hours = adjustment_hours
        else:
            return jsonify({'success': False, 'message': 'Invalid adjustment type'})

        # Calculate the actual adjustment needed
        actual_adjustment = new_hours - current_hours

        success, message = local_db.adjust_user_hours(name, actual_adjustment)

        if success:
            return jsonify({
                'success': True,
                'message': f'Successfully adjusted {name}\'s hours',
                'new_hours': round(new_hours, 2),
                'adjustment': round(actual_adjustment, 2)
            })
        else:
            return jsonify({'success': False, 'message': message})

    except ValueError as e:
        return jsonify({'success': False, 'message': 'Invalid hours value'})
    except Exception as e:
        logger.error(f"Error adjusting hours: {e}")
        return jsonify({
            'success': False,
            'message': f'Error adjusting hours: {str(e)}'
        })