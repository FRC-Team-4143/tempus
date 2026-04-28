#!/usr/bin/env python3
"""
Route handlers for Attendance Tracking System
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from flask import render_template, request, jsonify, redirect, url_for, flash, Response
from flask_httpauth import HTTPBasicAuth
import gspread
from google.oauth2.service_account import Credentials
from typing import Dict, List, Optional
import time
from threading import Lock, Thread

from .database import LocalDatabase, db_lock
from .utils import PRESET_NAMES, get_team_roster_mapping, load_names_from_file, get_category_mapping
from .connectivity import check_internet_connection, check_google_sheets_connection, check_slack_connection

logger = logging.getLogger(__name__)

# Global instances
local_db = LocalDatabase()

# Load environment variables
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

# HTTP Basic Auth setup
auth = HTTPBasicAuth()

@auth.verify_password
def verify_password(username, password):
    """Verify password for admin access - username can be anything, password must match ADMIN_PASSWORD"""
    admin_password = os.environ.get('ADMIN_PASSWORD', 'admin')
    return password == admin_password

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
        """Add an attendance record to local database only (Google Sheets sync happens in background)"""
        try:
            # Add to local database
            success = local_db.add_record(name, action)
            if not success:
                return False, "Failed to add record to local database"

            # Note: Google Sheets sync happens automatically in background every 5 minutes
            return True, f"Successfully recorded {action} for {name}"

        except Exception as e:
            logger.error(f"Error adding attendance record: {e}")
            return False, str(e)

    def sync_to_google_sheets(self):
        """Sync unsynced records to Google Sheets with enhanced column format:
        Timestamp | Name | Team | Category | Date | Time | Shift Duration | Total Hours | Notes
        """
        if not self.gc:
            logger.warning("Google Sheets not available, skipping sync")
            return
        
        # Check internet connectivity before attempting sync
        if not check_google_sheets_connection():
            logger.warning("⚠️ No internet connection, skipping Google Sheets sync")
            return

        try:
            # Get data from database with minimal lock time
            with db_lock:
                # Get unsynced records
                conn = sqlite3.connect(local_db.db_path)
                cursor = conn.cursor()
                cursor.execute('SELECT id, timestamp, name, action, duration_hours, notes FROM attendance_records WHERE synced = 0 ORDER BY timestamp')
                unsynced_records = cursor.fetchall()
                
                # Get current total hours for all users
                cursor.execute('SELECT name, total_hours FROM user_hours')
                user_hours_data = cursor.fetchall()
                conn.close()

            if not unsynced_records:
                return

            # Process data outside of db_lock
            team_mapping = get_team_roster_mapping()
            category_mapping = get_category_mapping()
            user_hours = {name: hours for name, hours in user_hours_data}

            # Open spreadsheet - this is slow, do it outside db_lock
            spreadsheet = self.gc.open(self.spreadsheet_name)
            worksheet = spreadsheet.worksheet(self.worksheet_name)

            # Get current values
            current_values = worksheet.get_all_values()
            current_rows = len(current_values)

            # Add headers if sheet is empty
            if current_rows == 0:
                headers = ['Timestamp', 'Name', 'Team', 'Category', 'Date', 'Time', 'Shift Duration', 'Total Hours', 'Notes']
                worksheet.append_row(headers)
                current_rows = 1
                logger.info("✅ Added headers to Google Sheets")

            # Prepare data for batch update
            batch_data = []
            synced_ids = []

            for record in unsynced_records:
                record_id, timestamp, name, action, duration_hours, notes = record

                # Convert timestamp to components
                try:
                    dt = datetime.fromisoformat(timestamp)
                    full_timestamp = dt.strftime('%Y-%m-%d %H:%M:%S')
                    date_only = dt.strftime('%Y-%m-%d')
                    time_only = dt.strftime('%H:%M:%S')
                except:
                    full_timestamp = timestamp
                    date_only = timestamp.split(' ')[0] if ' ' in timestamp else timestamp
                    time_only = timestamp.split(' ')[1] if ' ' in timestamp else ''

                # Get team information
                team = team_mapping.get(name, 'Unknown')
                category = category_mapping.get(name, '')

                # Format shift duration (only show on checkout)
                shift_duration = ""
                if action == 'check-out' and duration_hours > 0:
                    shift_duration = f"{duration_hours:.2f}h"

                # Get total hours for user
                total_hours = user_hours.get(name, 0)
                total_hours_str = f"{total_hours:.2f}h"

                # New column format: Timestamp, Name, Team, Category, Date, Time, Shift Duration, Total Hours, Notes
                row_data = [full_timestamp, name, team, category, date_only, time_only, shift_duration, total_hours_str, notes or '']
                batch_data.append(row_data)
                synced_ids.append(record_id)

            if batch_data:
                # Add rows to sheet - slow operation, outside db_lock
                start_row = current_rows + 1
                worksheet.append_rows(batch_data)

                # Mark records as synced - quick operation with lock
                local_db.mark_records_synced(synced_ids)

                logger.info(f"✅ Synced {len(batch_data)} records to Google Sheets with enhanced format")

        except Exception as e:
            logger.error(f"❌ Error syncing to Google Sheets: {e}")
            raise

# Global tracker instance
tracker = AttendanceTracker()

def background_sync():
    """Background thread to periodically sync to Google Sheets"""
    logger.info("🕐 Background Google Sheets sync thread started - will sync every 5 minutes")
    while True:
        try:
            time.sleep(300)  # Sync every 5 minutes
            
            # Check connectivity before attempting sync
            if not check_internet_connection():
                logger.debug("⚠️ No internet connection, skipping scheduled sync")
                continue
            
            logger.info("🔄 Starting scheduled Google Sheets sync...")
            tracker.sync_to_google_sheets()
            logger.info("✅ Scheduled Google Sheets sync completed")
        except Exception as e:
            logger.error(f"❌ Background sync error: {e}")
            time.sleep(60)  # Wait a minute before retrying

def midnight_signout():
    """Background thread to automatically sign out all users at midnight"""
    logger.info("🌙 Midnight sign-out thread started - will sign out all users at midnight")
    last_signout_date = None
    
    while True:
        try:
            now = datetime.now()
            current_date = now.date()
            
            # Check if it's midnight (between 12:00 AM and 12:01 AM) and we haven't signed out today yet
            if (now.hour == 0 and now.minute == 0 and 
                (last_signout_date is None or last_signout_date != current_date)):
                
                logger.info("🌙 Midnight reached - automatically signing out all checked-in users")
                
                # Get team roster mapping
                team_mapping = get_team_roster_mapping()
                
                # Get today's records to find checked-in users
                today = current_date.strftime('%Y-%m-%d')
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
                
                if checked_in_users:
                    logger.info(f"🌙 Found {len(checked_in_users)} users still checked in at midnight: {checked_in_users}")
                    
                    # Sign out each checked-in user
                    checked_out_count = 0
                    
                    for name in checked_in_users:
                        # Use local_db.add_record directly to avoid Google Sheets sync for mass operations
                        success = local_db.add_record(name, 'check-out')
                        if success:
                            checked_out_count += 1
                            logger.info(f"🌙 Midnight sign-out: {name}")
                        else:
                            logger.error(f"❌ Failed to sign out {name} at midnight")
                    
                    logger.info(f"🌙 Midnight sign-out completed: {checked_out_count}/{len(checked_in_users)} users signed out")
                    last_signout_date = current_date
                else:
                    logger.info("🌙 Midnight check: No users currently checked in")
                    last_signout_date = current_date
            
            # Sleep for 60 seconds before checking again
            time.sleep(60)
            
        except Exception as e:
            logger.error(f"❌ Error in midnight sign-out thread: {e}")
            time.sleep(60)  # Wait a minute before retrying

# Start background sync thread
sync_thread = Thread(target=background_sync, daemon=True)
sync_thread.start()

# Start midnight sign-out thread
midnight_thread = Thread(target=midnight_signout, daemon=True)
midnight_thread.start()

# Flask route handlers
def index():
    """Main attendance page"""
    return render_template('index.html')

@auth.login_required
def admin():
    """Admin dashboard - protected by HTTP Basic Authentication"""
    today = datetime.now().strftime('%Y-%m-%d')
    records = local_db.get_records(date_filter=today)
    today_summary = {
        'date': today,
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
            'total_check_ins': len([r for r in records if r['Action'] == 'check-in']),
            'total_check_outs': len([r for r in records if r['Action'] == 'check-out']),
            'unique_users': len(set(r['Name'] for r in records))
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
    """Health check endpoint with connectivity status"""
    status = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'connectivity': {
            'internet': check_internet_connection(),
            'google_sheets': check_google_sheets_connection() if gc else False,
            'slack': check_slack_connection()
        },
        'services': {
            'database': True,  # If we got here, database is working
            'google_sheets_configured': gc is not None,
            'slack_configured': os.environ.get('SLACK_ENABLED', 'False').lower() == 'true'
        }
    }
    return jsonify(status)

def quick_status():
    """Quick status overview"""
    try:
        # Get all records to determine current status
        records = local_db.get_records()

        # Find the most recent action for each user to determine current status
        user_last_action = {}
        for record in records:
            name = record['Name']
            timestamp = record['Timestamp']
            action = record['Action']
            
            # Keep track of the most recent action for each user
            if name not in user_last_action or timestamp > user_last_action[name]['timestamp']:
                user_last_action[name] = {
                    'timestamp': timestamp,
                    'action': action
                }

        # Count users whose last action was check-in
        checked_in_count = sum(1 for user_data in user_last_action.values() 
                              if user_data['action'] == 'check-in')

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
        # Load names from file to ensure PRESET_NAMES is up to date
        load_names_from_file()
        
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

def get_slack_test_users():
    """Get list of users and mentors for Slack test notifications"""
    try:
        # Get students from PRESET_NAMES
        team_mapping = get_team_roster_mapping()
        users_list = []
        
        # Add students
        for name in PRESET_NAMES:
            if not name.startswith('--- Team'):
                team = team_mapping.get(name, 'unknown')
                users_list.append({
                    'name': name,
                    'team': team,
                    'type': 'student'
                })
        
        # Add mentors from mentors.csv
        mentors_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'mentors.csv')
        if os.path.exists(mentors_path):
            with open(mentors_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('"TeamNumber"'):  # Skip header
                        # Parse CSV: "TeamNumber","Category","MentorName","SlackUserID"
                        parts = [part.strip('"') for part in line.split(',')]
                        if len(parts) >= 4:
                            team = parts[0]
                            category = parts[1]
                            mentor_name = parts[2]
                            slack_id = parts[3]
                            
                            if slack_id and mentor_name:  # Only add if has Slack ID and name
                                # Avoid duplicates
                                if not any(u['name'] == mentor_name for u in users_list):
                                    users_list.append({
                                        'name': mentor_name,
                                        'team': team,
                                        'type': 'mentor',
                                        'category': category
                                    })
        
        # Sort by type (students first), then by name
        users_list.sort(key=lambda x: (x['type'] == 'mentor', x['name']))
        
        return jsonify({'success': True, 'users': users_list})
        
    except Exception as e:
        logger.error(f"Error loading users for Slack test: {e}")
        return jsonify({'success': False, 'error': str(e)})

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
        # Save uploaded file as users.csv
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        file.save(os.path.join(data_dir, 'users.csv'))

        # Reload names
        load_names_from_file()

        return jsonify({'success': True, 'message': 'Users uploaded successfully', 'names': PRESET_NAMES})

    except Exception as e:
        logger.error(f"Error uploading names: {e}")
        return jsonify({'success': False, 'message': 'Error uploading file'})

def add_name():
    """Add a name with team, category, and slack UID to the preset list"""
    data = request.get_json()
    name = data.get('name', '').strip()
    team = data.get('team', '').strip()
    category = data.get('category', '').strip()
    slack_uid = data.get('slack_uid', '').strip()

    if not name:
        return jsonify({'success': False, 'message': 'Name is required'})
    
    if not team:
        return jsonify({'success': False, 'message': 'Team is required'})
    
    if not category:
        return jsonify({'success': False, 'message': 'Category is required'})

    # Check if name already exists in the flat list (excluding team headers)
    existing_names = [n for n in PRESET_NAMES if not n.startswith('--- Team')]
    if name in existing_names:
        return jsonify({'success': False, 'message': 'Name already exists'})

    # Add to users.csv file
    try:
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        users_path = os.path.join(data_dir, 'users.csv')
        with open(users_path, 'a', encoding='utf-8') as f:
            f.write(f'"{name}","{team}","{category}","{slack_uid}"\n')
        
        # Reload names from file to update PRESET_NAMES with proper grouping
        from .utils import load_names_from_file
        load_names_from_file()
        
        logger.info(f"Added user: {name} (Team {team}, {category}, Slack: {slack_uid})")
        return jsonify({'success': True, 'message': f'Added {name} to the list', 'names': PRESET_NAMES})
        
    except Exception as e:
        logger.error(f"Could not save user to file: {e}")
        return jsonify({'success': False, 'message': f'Error saving user: {str(e)}'})

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
        logger.info("Starting mass sign-out operation")
        # Get team roster mapping
        team_mapping = get_team_roster_mapping()
        logger.info(f"Loaded team mapping with {len(team_mapping)} entries")

        # Get today's records to find checked-in users
        today = datetime.now().strftime('%Y-%m-%d')
        all_records = local_db.get_records(date_filter=today)
        logger.info(f"Retrieved {len(all_records)} records for today")

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
        logger.info(f"Found {len(checked_in_users)} users currently checked in: {checked_in_users}")

        if not checked_in_users:
            logger.info("No users to sign out")
            return jsonify({
                'success': True,
                'message': 'No users currently checked in',
                'count': 0,
                'users': []
            })

        # Sign out each checked-in user
        logger.info("Starting individual sign-out operations")
        checked_out_count = 0
        device_ip = request.remote_addr

        for name in checked_in_users:
            # Use local_db.add_record directly to avoid Google Sheets sync for mass operations
            success = local_db.add_record(name, 'check-out')
            if success:
                checked_out_count += 1
                logger.info(f"Admin signed out {name}")
            else:
                logger.error(f"Failed to sign out {name}")

        # All sign-outs completed - background sync will handle Google Sheets update
        logger.info(f"Mass sign-out completed successfully: {checked_out_count}/{len(checked_in_users)} users signed out")
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
        # Check connectivity first
        if not check_internet_connection():
            return jsonify({
                'success': False, 
                'message': 'No internet connection available',
                'offline': True
            })
        
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

def api_weekly_attendance():
    """Get weekly attendance metrics for all users"""
    try:
        weeks_back = int(request.args.get('weeks_back', 0))
        
        weekly_data = local_db.get_weekly_attendance(weeks_back)
        
        # Get team mapping for grouping
        team_mapping = get_team_roster_mapping()
        
        # Group by team
        teams = {'4143': [], '4423': []}
        summary = {
            'team_4143_total': 0, 'team_4143_meeting_requirement': 0,
            'team_4423_total': 0, 'team_4423_meeting_requirement': 0,
            'overall_total': 0, 'overall_meeting_requirement': 0
        }
        
        for name, data in weekly_data.items():
            team = team_mapping.get(name, '4143')
            if team in teams:
                teams[team].append({'name': name, **data})
                summary[f'team_{team}_total'] += 1
                if data['status'] == 'good':
                    summary[f'team_{team}_meeting_requirement'] += 1
        
        summary['overall_total'] = summary['team_4143_total'] + summary['team_4423_total']
        summary['overall_meeting_requirement'] = summary['team_4143_meeting_requirement'] + summary['team_4423_meeting_requirement']
        
        # Sort users within teams by attendance percentage (descending)
        for team_users in teams.values():
            team_users.sort(key=lambda x: x['attendance_percentage'], reverse=True)
        
        return jsonify({
            'success': True,
            'teams': teams,
            'summary': summary,
            'weeks_back': weeks_back
        })
        
    except Exception as e:
        logger.error(f"Error getting weekly attendance: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_slack_notify():
    """Manually trigger Slack notifications for users not meeting attendance requirements"""
    try:
        from .slack_notifier import SlackNotifier
        
        # Get data from request body if present, otherwise from query params (backwards compatibility)
        if request.is_json:
            data = request.get_json()
            weeks_back = data.get('weeks_back', 0)
            custom_message = data.get('custom_message')
        else:
            weeks_back = int(request.args.get('weeks_back', 0))
            custom_message = None
        
        notifier = SlackNotifier()
        result = notifier.check_and_notify_weekly_attendance(weeks_back, custom_message)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error triggering Slack notifications: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_slack_test():
    """Send a test Slack notification to a specific user"""
    try:
        from .slack_notifier import SlackNotifier
        
        data = request.get_json()
        user_name = data.get('name', '').strip()
        
        if not user_name:
            return jsonify({'success': False, 'message': 'Name is required'})
        
        notifier = SlackNotifier()
        result = notifier.send_test_notification(user_name)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error sending test notification: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_get_all_records():
    """Get all attendance records with optional filters"""
    try:
        name = request.args.get('name', '').strip()
        date_from = request.args.get('date_from', '').strip()
        date_to = request.args.get('date_to', '').strip()
        limit = int(request.args.get('limit', 100))

        records = local_db.get_all_records_with_filters(
            name=name if name else None,
            date_from=date_from if date_from else None,
            date_to=date_to if date_to else None,
            limit=limit
        )

        return jsonify({'success': True, 'records': records})

    except Exception as e:
        logger.error(f"Error getting all records: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_get_record():
    """Get a specific record by ID"""
    try:
        record_id = int(request.args.get('id'))
        record = local_db.get_record_by_id(record_id)

        if record:
            return jsonify({'success': True, 'record': record})
        else:
            return jsonify({'success': False, 'message': 'Record not found'})

    except Exception as e:
        logger.error(f"Error getting record: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_update_record():
    """Update an existing attendance record"""
    try:
        data = request.get_json()
        record_id = int(data.get('id'))
        timestamp = data.get('timestamp', '').strip()
        name = data.get('name', '').strip()
        action = data.get('action', '').strip()
        notes = data.get('notes', '').strip()

        if not all([record_id, timestamp, name, action]):
            return jsonify({'success': False, 'message': 'Missing required fields'})

        if action not in ['check-in', 'check-out']:
            return jsonify({'success': False, 'message': 'Invalid action. Must be check-in or check-out'})

        success, message = local_db.update_record(record_id, timestamp, name, action, notes)

        return jsonify({'success': success, 'message': message})

    except Exception as e:
        logger.error(f"Error updating record: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_delete_record():
    """Delete an attendance record"""
    try:
        data = request.get_json()
        record_id = int(data.get('id'))

        if not record_id:
            return jsonify({'success': False, 'message': 'Record ID is required'})

        success, message = local_db.delete_record(record_id)

        return jsonify({'success': success, 'message': message})

    except Exception as e:
        logger.error(f"Error deleting record: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_recalculate_durations():
    """Recalculate missing durations for all check-out records"""
    try:
        count = local_db.recalculate_missing_durations()
        return jsonify({
            'success': True, 
            'message': f'Recalculated durations for {count} records',
            'count': count
        })

    except Exception as e:
        logger.error(f"Error recalculating durations: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_add_manual_record():
    """Add a manual attendance record with custom timestamp"""
    try:
        data = request.get_json()
        name = data.get('name')
        action = data.get('action')
        timestamp = data.get('timestamp')
        notes = data.get('notes', '')

        if not all([name, action, timestamp]):
            return jsonify({'success': False, 'message': 'Name, action, and timestamp are required'})

        if action not in ['check-in', 'check-out']:
            return jsonify({'success': False, 'message': 'Action must be check-in or check-out'})

        # Validate timestamp format
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid timestamp format. Use ISO format (YYYY-MM-DDTHH:MM:SS)'})

        success = local_db.add_manual_record(name, action, timestamp, notes)

        if success:
            return jsonify({'success': True, 'message': f'Successfully added {action} record for {name}'})
        else:
            return jsonify({'success': False, 'message': 'Failed to add record to database'})

    except Exception as e:
        logger.error(f"Error adding manual record: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_add_manual_session():
    """Add a complete manual session with both check-in and check-out"""
    try:
        data = request.get_json()
        name = data.get('name')
        sign_in_timestamp = data.get('sign_in_timestamp')
        sign_out_timestamp = data.get('sign_out_timestamp')
        notes = data.get('notes', '')

        if not all([name, sign_in_timestamp, sign_out_timestamp]):
            return jsonify({'success': False, 'message': 'Name, sign-in timestamp, and sign-out timestamp are required'})

        # Validate timestamp formats
        try:
            sign_in_dt = datetime.fromisoformat(sign_in_timestamp)
            sign_out_dt = datetime.fromisoformat(sign_out_timestamp)
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid timestamp format. Use ISO format (YYYY-MM-DDTHH:MM:SS)'})

        # Validate that sign-out is after sign-in
        if sign_out_dt <= sign_in_dt:
            return jsonify({'success': False, 'message': 'Sign-out time must be after sign-in time'})

        success = local_db.add_manual_session(name, sign_in_timestamp, sign_out_timestamp, notes)

        if success:
            return jsonify({'success': True, 'message': f'Successfully added session for {name}'})
        else:
            return jsonify({'success': False, 'message': 'Failed to add session to database'})

    except Exception as e:
        logger.error(f"Error adding manual session: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_add_manual_record():
    """Add a manual attendance record with custom timestamp"""
    try:
        data = request.get_json()
        name = data.get('name')
        action = data.get('action')
        timestamp = data.get('timestamp')
        notes = data.get('notes', '')

        if not all([name, action, timestamp]):
            return jsonify({'success': False, 'message': 'Name, action, and timestamp are required'})

        if action not in ['check-in', 'check-out']:
            return jsonify({'success': False, 'message': 'Action must be check-in or check-out'})

        # Validate timestamp format
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid timestamp format. Use ISO format (YYYY-MM-DDTHH:MM:SS)'})

        success = local_db.add_manual_record(name, action, timestamp, notes)

        if success:
            return jsonify({'success': True, 'message': f'Successfully added {action} record for {name}'})
        else:
            return jsonify({'success': False, 'message': 'Failed to add record'})

    except Exception as e:
        logger.error(f"Error adding manual record: {e}")
        return jsonify({'success': False, 'error': str(e)})

def api_recalculate_durations():
    """Recalculate missing durations for check-out records"""
    try:
        logger.info("🔧 Manual duration recalculation triggered")
        
        updated_count = local_db.recalculate_missing_durations()
        
        if updated_count > 0:
            message = f"Successfully recalculated durations for {updated_count} records."
        else:
            message = "No missing durations found - all records are up to date."
            
        return jsonify({
            'success': True, 
            'message': message,
            'updated_count': updated_count
        })
        
    except Exception as e:
        logger.error(f"Error during duration recalculation: {e}")
        return jsonify({'success': False, 'error': str(e)})

def api_verify_hours_consistency():
    """Verify and fix hours consistency between user_hours and attendance_records tables"""
    try:
        logger.info("🔧 Manual hours consistency check triggered")
        
        # First recalculate any missing durations
        missing_count = local_db.recalculate_missing_durations()
        
        # Then verify hours consistency
        is_consistent = local_db.verify_hours_consistency()
        
        message = "Hours consistency verified and any issues have been auto-fixed."
        if missing_count > 0:
            message += f" Fixed {missing_count} missing duration calculations."
            
        return jsonify({
            'success': True, 
            'message': message,
            'was_consistent': is_consistent,
            'missing_durations_fixed': missing_count
        })
        
    except Exception as e:
        logger.error(f"Error during hours consistency check: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Hour Requirements API Endpoints

def api_get_hour_requirements():
    """Get all hour requirements (defaults and overrides) - Public endpoint for displaying current requirements"""
    try:
        requirements = local_db.get_all_hour_requirements()
        return jsonify({
            'success': True,
            'defaults': requirements['defaults'],
            'overrides': requirements['overrides']
        })
    except Exception as e:
        logger.error(f"Error getting hour requirements: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_set_default_hours():
    """Update default hours for a team"""
    try:
        data = request.get_json()
        team_number = str(data.get('team_number', ''))
        hours = float(data.get('hours', 0))
        
        if team_number not in ['4143', '4423']:
            return jsonify({'success': False, 'error': 'Invalid team number. Must be 4143 or 4423'})
        
        if hours < 0:
            return jsonify({'success': False, 'error': 'Hours must be non-negative'})
        
        success = local_db.set_default_hours(team_number, hours)
        
        if success:
            logger.info(f"✅ Admin updated default hours for team {team_number}: {hours}")
            return jsonify({
                'success': True,
                'message': f'Successfully updated default hours for team {team_number}'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to update default hours'})
            
    except ValueError as e:
        return jsonify({'success': False, 'error': 'Invalid hours value. Must be a number'})
    except Exception as e:
        logger.error(f"Error setting default hours: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_add_hour_requirement():
    """Add a new hour requirement override"""
    try:
        data = request.get_json()
        team_number = str(data.get('team_number', ''))
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        required_hours = float(data.get('required_hours', 0))
        description = data.get('description', '')
        
        success, message = local_db.add_hour_requirement(
            team_number, start_date, end_date, required_hours, description
        )
        
        if success:
            logger.info(f"✅ Admin added hour requirement: {message}")
        
        return jsonify({'success': success, 'message': message})
        
    except ValueError as e:
        return jsonify({'success': False, 'error': f'Invalid input: {str(e)}'})
    except Exception as e:
        logger.error(f"Error adding hour requirement: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_update_hour_requirement():
    """Update an existing hour requirement override"""
    try:
        data = request.get_json()
        requirement_id = int(data.get('id', 0))
        team_number = str(data.get('team_number', ''))
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        required_hours = float(data.get('required_hours', 0))
        description = data.get('description', '')
        
        success, message = local_db.update_hour_requirement(
            requirement_id, team_number, start_date, end_date, required_hours, description
        )
        
        if success:
            logger.info(f"✅ Admin updated hour requirement ID {requirement_id}")
        
        return jsonify({'success': success, 'message': message})
        
    except ValueError as e:
        return jsonify({'success': False, 'error': f'Invalid input: {str(e)}'})
    except Exception as e:
        logger.error(f"Error updating hour requirement: {e}")
        return jsonify({'success': False, 'error': str(e)})

@auth.login_required
def api_delete_hour_requirement():
    """Delete an hour requirement override"""
    try:
        data = request.get_json()
        requirement_id = int(data.get('id', 0))
        
        success, message = local_db.delete_hour_requirement(requirement_id)
        
        if success:
            logger.info(f"✅ Admin deleted hour requirement ID {requirement_id}")
        
        return jsonify({'success': success, 'message': message})
        
    except ValueError as e:
        return jsonify({'success': False, 'error': f'Invalid input: {str(e)}'})
    except Exception as e:
        logger.error(f"Error deleting hour requirement: {e}")
        return jsonify({'success': False, 'error': str(e)})
