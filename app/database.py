#!/usr/bin/env python3
"""
Database module for Attendance Tracking System
Contains database classes and operations
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import hashlib
from threading import Lock
import json
from collections import defaultdict
from dotenv import load_dotenv

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

# Database lock to prevent race conditions between user operations and background sync
db_lock = Lock()

# Configure logging
logger = logging.getLogger(__name__)

class LocalDatabase:
    def __init__(self):
        # Get database path from environment or use default
        db_path = os.environ.get('DATABASE_PATH', 'data/attendance.db')
        
        # If relative path, make it relative to project root
        if not os.path.isabs(db_path):
            project_root = os.path.join(os.path.dirname(__file__), '..')
            db_path = os.path.join(project_root, db_path)
        
        # Ensure directory exists
        db_dir = os.path.dirname(db_path)
        os.makedirs(db_dir, exist_ok=True)
        
        self.db_path = os.path.abspath(db_path)
        logger.info(f"📁 Using database: {self.db_path}")
        self.init_database()

    def init_database(self):
        """Initialize the SQLite database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Enable WAL mode for better concurrency and crash recovery
            cursor.execute('PRAGMA journal_mode=WAL')
            # Ensure data is synced to disk (FULL = safest, NORMAL = good balance)
            cursor.execute('PRAGMA synchronous=FULL')
            logger.debug("✅ WAL mode and synchronous writes enabled")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attendance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    duration_hours REAL DEFAULT 0,
                    notes TEXT,
                    synced BOOLEAN DEFAULT 0
                )
            ''')

            # Migration: Add notes column if it doesn't exist
            try:
                cursor.execute("SELECT notes FROM attendance_records LIMIT 1")
            except sqlite3.OperationalError:
                # Column doesn't exist, add it
                cursor.execute("ALTER TABLE attendance_records ADD COLUMN notes TEXT")
                logger.info("✅ Added notes column to attendance_records table")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_hours (
                    name TEXT PRIMARY KEY,
                    total_hours REAL DEFAULT 0,
                    last_checkin TEXT,
                    session_count INTEGER DEFAULT 0,
                    last_activity TEXT
                )
            ''')

            conn.commit()
            conn.close()
            logger.debug("✅ Local database initialized successfully")
        except Exception as e:
            logger.error(f"❌ Error initializing database: {e}")

    def initialize_user_hours(self, names_list: List[str]):
        """Initialize user_hours records for all names in the list"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                today = datetime.now().strftime('%Y-%m-%d')
                initialized_count = 0

                for name in names_list:
                    # Skip team headers
                    if name.startswith('---'):
                        continue

                    # Check if user already exists
                    cursor.execute('SELECT name FROM user_hours WHERE name = ?', (name,))
                    if not cursor.fetchone():
                        cursor.execute('''
                            INSERT INTO user_hours (name, total_hours, session_count, last_activity)
                            VALUES (?, 0, 0, ?)
                        ''', (name, today))
                        initialized_count += 1

                conn.commit()
                conn.close()

                if initialized_count > 0:
                    logger.debug(f"✅ Initialized {initialized_count} new user records")

            except Exception as e:
                logger.error(f"❌ Error initializing user hours: {e}")

    def cleanup_old_users(self, current_names: List[str]):
        """Remove user_hours records for names that are no longer in the current list"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Get all names currently in the database
                cursor.execute('SELECT name FROM user_hours')
                db_names = [row[0] for row in cursor.fetchall()]

                # Filter out team headers from current names
                actual_names = [name for name in current_names if not name.startswith('---')]

                # Find names to remove (in DB but not in current list)
                names_to_remove = [name for name in db_names if name not in actual_names]

                if names_to_remove:
                    # Remove from user_hours table
                    cursor.executemany('DELETE FROM user_hours WHERE name = ?', [(name,) for name in names_to_remove])

                    # Also remove attendance records for these users
                    cursor.executemany('DELETE FROM attendance_records WHERE name = ?', [(name,) for name in names_to_remove])

                    conn.commit()
                    logger.info(f"🧹 Cleaned up {len(names_to_remove)} old user records: {', '.join(names_to_remove)}")

                conn.close()

            except Exception as e:
                logger.error(f"❌ Error cleaning up old users: {e}")

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

                elif action == 'check-out':
                    # Find the most recent check-in for this user by searching attendance_records
                    cursor.execute('''
                        SELECT timestamp FROM attendance_records
                        WHERE name = ? AND action = 'check-in' AND timestamp < ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    ''', (name, timestamp))
                    
                    checkin_record = cursor.fetchone()
                    
                    if checkin_record:
                        # Calculate session duration
                        try:
                            checkin_time = datetime.fromisoformat(checkin_record[0])
                            checkout_time = datetime.fromisoformat(timestamp)
                            duration_seconds = (checkout_time - checkin_time).total_seconds()
                            duration_hours = duration_seconds / 3600

                            if 0 < duration_hours < 24:  # Sanity check
                                # Update total hours and session count
                                new_total_hours = total_hours + duration_hours
                                new_session_count = session_count + 1
                                
                                # Update user_hours table
                                cursor.execute('''
                                    UPDATE user_hours
                                    SET total_hours = ?, last_checkin = NULL, session_count = ?, last_activity = ?
                                    WHERE name = ?
                                ''', (new_total_hours, new_session_count, timestamp[:10], name))
                                
                                # Verify the update was successful
                                if cursor.rowcount == 0:
                                    logger.error(f"Failed to update user_hours for {name} - user not found in table")
                                else:
                                    logger.info(f"Session complete for {name}: {duration_hours:.2f} hours (Total: {new_total_hours:.2f}h)")
                            else:
                                logger.warning(f"Invalid session duration for {name}: {duration_hours:.3f} hours (outside 0-24h range)")
                        except Exception as e:
                            logger.error(f"Error calculating duration for {name}: {e}")
                    else:
                        logger.warning(f"No matching check-in found for {name}'s check-out at {timestamp}")

                # Insert attendance record
                cursor.execute('''
                    INSERT INTO attendance_records (timestamp, name, action, duration_hours, notes)
                    VALUES (?, ?, ?, ?, NULL)
                ''', (timestamp, name, action, duration_hours))

                conn.commit()
                conn.close()
                return True

            except Exception as e:
                logger.error(f"Error adding record: {e}")
                return False

    def add_manual_record(self, name: str, action: str, timestamp: str, notes: str = '') -> bool:
        """Add a manual record with custom timestamp to the local database and update hours tracking"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

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

                elif action == 'check-out':
                    # Find the most recent check-in for this user by searching attendance_records
                    cursor.execute('''
                        SELECT timestamp FROM attendance_records
                        WHERE name = ? AND action = 'check-in' AND timestamp < ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    ''', (name, timestamp))
                    
                    checkin_record = cursor.fetchone()
                    
                    if checkin_record:
                        # Calculate session duration
                        try:
                            checkin_time = datetime.fromisoformat(checkin_record[0])
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

                                logger.info(f"Manual session complete for {name}: {duration_hours:.2f} hours (Total: {new_total_hours:.2f}h)")
                        except Exception as e:
                            logger.error(f"Error calculating duration for {name}: {e}")
                    else:
                        logger.warning(f"No matching check-in found for {name}'s check-out at {timestamp}")

                # Insert attendance record
                cursor.execute('''
                    INSERT INTO attendance_records (timestamp, name, action, duration_hours, notes)
                    VALUES (?, ?, ?, ?, ?)
                ''', (timestamp, name, action, duration_hours, notes))

                conn.commit()
                conn.close()
                return True

            except Exception as e:
                logger.error(f"Error adding manual record: {e}")
                return False

    def add_manual_session(self, name: str, sign_in_timestamp: str, sign_out_timestamp: str, notes: str = '') -> bool:
        """Add a complete manual session with both check-in and check-out records"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # First, add the check-in record
                checkin_success = self._add_single_record(cursor, name, 'check-in', sign_in_timestamp, notes)
                if not checkin_success:
                    conn.rollback()
                    return False

                # Then, add the check-out record
                checkout_success = self._add_single_record(cursor, name, 'check-out', sign_out_timestamp, notes)
                if not checkout_success:
                    conn.rollback()
                    return False

                conn.commit()
                conn.close()
                logger.info(f"Successfully added manual session for {name}: {sign_in_timestamp} to {sign_out_timestamp}")
                return True

            except Exception as e:
                logger.error(f"Error adding manual session: {e}")
                return False

    def _add_single_record(self, cursor, name: str, action: str, timestamp: str, notes: str = '') -> bool:
        """Helper method to add a single record within a transaction"""
        try:
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

            elif action == 'check-out':
                # Find the most recent check-in for this user by searching attendance_records
                cursor.execute('''
                    SELECT timestamp FROM attendance_records
                    WHERE name = ? AND action = 'check-in' AND timestamp < ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                ''', (name, timestamp))

                checkin_record = cursor.fetchone()

                if checkin_record:
                    # Calculate session duration
                    try:
                        checkin_time = datetime.fromisoformat(checkin_record[0])
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

                            logger.info(f"Manual session complete for {name}: {duration_hours:.2f} hours (Total: {new_total_hours:.2f}h)")
                    except Exception as e:
                        logger.error(f"Error calculating duration for {name}: {e}")
                else:
                    logger.warning(f"No matching check-in found for {name}'s check-out at {timestamp}")

            # Insert attendance record
            cursor.execute('''
                INSERT INTO attendance_records (timestamp, name, action, duration_hours, notes)
                VALUES (?, ?, ?, ?, ?)
            ''', (timestamp, name, action, duration_hours, notes))

            return True

        except Exception as e:
            logger.error(f"Error adding single record: {e}")
            return False

    def get_user_status(self, name: str) -> str:
        """Get the current status of a user for today"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Get today's date
                today = datetime.now().strftime('%Y-%m-%d')

                cursor.execute('''
                    SELECT action FROM attendance_records
                    WHERE name = ? AND date(timestamp) = ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                ''', (name, today))

                result = cursor.fetchone()
                conn.close()

                if result:
                    return 'checked-in' if result[0] == 'check-in' else 'checked-out'
                return 'checked-out'
            except Exception as e:
                logger.error(f"Error getting user status: {e}")
                return 'checked-out'

    def get_records(self, date_filter=None, name_filter=None) -> List[Dict]:
        """Get attendance records with optional filters"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                query = '''
                    SELECT timestamp, name, action, duration_hours
                    FROM attendance_records
                    WHERE 1=1
                '''
                params = []

                if date_filter:
                    query += ' AND date(timestamp) = ?'
                    params.append(date_filter)

                if name_filter:
                    query += ' AND name = ?'
                    params.append(name_filter)

                query += ' ORDER BY timestamp DESC'

                cursor.execute(query, params)
                rows = cursor.fetchall()
                conn.close()

                records = []
                for row in rows:
                    records.append({
                        'Timestamp': row[0],
                        'Name': row[1],
                        'Action': row[2],
                        'Duration (hours)': f"{row[3]:.2f}h" if row[3] > 0 else '',
                        'Notes': '',  # No notes field in current schema
                        'Device IP': ''  # No device IP field in current schema
                    })

                return records

            except Exception as e:
                logger.error(f"Error getting records: {e}")
                return []

    def get_leaderboard_data(self) -> List[Dict]:
        """Get leaderboard data from user_hours table"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT name, total_hours, session_count, last_activity
                    FROM user_hours
                    ORDER BY total_hours DESC
                    LIMIT 50
                ''')

                rows = cursor.fetchall()
                conn.close()

                leaderboard = []
                for i, row in enumerate(rows, 1):  # Start rank from 1
                    leaderboard.append({
                        'name': row[0],
                        'total_hours': round(row[1], 2),
                        'sessions': row[2],
                        'last_activity': row[3] or 'Never',
                        'rank': i
                    })

                return leaderboard

            except Exception as e:
                logger.error(f"Error getting leaderboard data: {e}")
                return []

    def mark_records_synced(self, record_ids: List[int]):
        """Mark records as synced to Google Sheets"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                placeholders = ','.join('?' * len(record_ids))
                cursor.execute(f'UPDATE attendance_records SET synced = 1 WHERE id IN ({placeholders})', record_ids)

                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Error marking records as synced: {e}")

    def get_weekly_attendance(self, weeks_back: int = 0) -> Dict[str, Dict]:
        """Get weekly attendance metrics for all users
        
        Args:
            weeks_back: Number of weeks back from current week (0 = current week)
        
        Returns:
            Dict with user names as keys and attendance data as values
        """
        with db_lock:
            try:
                # Import team mapping here to avoid circular imports
                from .utils import get_team_roster_mapping, get_category_mapping
                
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Get team mapping
                team_mapping = get_team_roster_mapping()
                category_mapping = get_category_mapping()

                # Calculate the week start (Monday) for the requested week
                today = datetime.now()
                today = today - timedelta(weeks=weeks_back)
                
                # Find Monday of the current week
                week_start = today - timedelta(days=today.weekday())  # Monday
                week_start_str = week_start.strftime('%Y-%m-%d')
                week_end = week_start + timedelta(days=6)  # Sunday
                week_end_str = week_end.strftime('%Y-%m-%d')

                # Get all attendance records for the week
                cursor.execute('''
                    SELECT name, timestamp, action, duration_hours
                    FROM attendance_records
                    WHERE date(timestamp) BETWEEN ? AND ?
                    ORDER BY name, timestamp
                ''', (week_start_str, week_end_str))

                records = cursor.fetchall()

                # Get total hours for all users
                cursor.execute('SELECT name, total_hours FROM user_hours')
                user_total_hours = {name: hours for name, hours in cursor.fetchall()}

                conn.close()

                # Process records by user
                weekly_data = {}
                
                # Group records by user
                user_records = defaultdict(list)
                for name, timestamp, action, duration_hours in records:
                    if name and not name.startswith('---'):
                        user_records[name].append({
                            'timestamp': timestamp,
                            'action': action,
                            'duration_hours': duration_hours
                        })

                # Calculate attendance metrics for each user
                for name, user_recs in user_records.items():
                    # Calculate total hours for the week
                    total_weekly_hours = 0
                    sessions_completed = 0
                    
                    # Group by sessions (check-in to check-out pairs)
                    sessions = []
                    current_session = None
                    
                    for record in user_recs:
                        if record['action'] == 'check-in':
                            current_session = {'checkin': record['timestamp'], 'duration': 0}
                        elif record['action'] == 'check-out' and current_session:
                            # Calculate session duration
                            try:
                                checkin_time = datetime.fromisoformat(current_session['checkin'])
                                checkout_time = datetime.fromisoformat(record['timestamp'])
                                duration = (checkout_time - checkin_time).total_seconds() / 3600
                                
                                if 0 < duration < 24:  # Sanity check
                                    current_session['duration'] = duration
                                    sessions.append(current_session)
                                    total_weekly_hours += duration
                                    sessions_completed += 1
                            except:
                                pass
                            
                            current_session = None
                        elif record['action'] == 'manual_adjustment':
                            # Add manual adjustment directly to weekly hours
                            total_weekly_hours += record['duration_hours']

                    # Calculate attendance percentage
                    # Check if we're before the expected hours start date
                    from .utils import get_expected_hours_config
                    config = get_expected_hours_config()
                    start_date = config['start_date']
                    
                    if week_start < start_date:
                        required_hours = 0.0
                    else:
                        required_hours = 11.0
                    
                    attendance_percentage = min(100.0, (total_weekly_hours / required_hours) * 100) if required_hours > 0 else 0
                    
                    # Calculate total expected hours up to the end of this week
                    from .utils import calculate_total_expected_hours, calculate_week_number, get_expected_hours_config
                    total_expected_hours = calculate_total_expected_hours(week_end)
                    # Use the expected hours for this specific week for comparison
                    config = get_expected_hours_config()
                    if week_start < config['start_date']:
                        total_expected_hours = 0.0
                    else:
                        week_number = calculate_week_number(week_start)
                        total_expected_hours = 11.0 * week_number
                    total_hours_ratio = round((user_total_hours.get(name, 0) / total_expected_hours * 100), 1) if total_expected_hours > 0 else 0
                    
                    # Determine total status (similar to weekly)
                    if total_hours_ratio >= 80:
                        total_status = 'good'
                    elif total_hours_ratio >= 60:
                        total_status = 'warning'
                    else:
                        total_status = 'danger'
                    
                    # Determine status
                    if attendance_percentage >= 80:
                        status = 'good'
                    elif attendance_percentage >= 60:
                        status = 'warning'
                    else:
                        status = 'danger'
                    
                    weekly_data[name] = {
                        'total_hours': round(total_weekly_hours, 2),
                        'required_hours': required_hours,
                        'attendance_percentage': round(attendance_percentage, 1),
                        'sessions_completed': sessions_completed,
                        'status': status,
                        'week_start': week_start_str,
                        'week_end': week_end_str,
                        'team': team_mapping.get(name, '4143'),  # Add team information
                        'category': category_mapping.get(name, ''),  # Add category information
                        'sessions': sessions,
                        'all_time_hours': round(user_total_hours.get(name, 0), 2),  # Add total hours
                        'total_expected_hours': total_expected_hours,
                        'total_hours_ratio': total_hours_ratio,
                        'total_status': total_status
                    }

                return weekly_data

            except Exception as e:
                logger.error(f"Error calculating weekly attendance: {e}")
                return {}

    def get_record_by_id(self, record_id: int) -> Optional[Dict]:
        """Get a specific attendance record by ID"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT id, timestamp, name, action, duration_hours, notes
                    FROM attendance_records
                    WHERE id = ?
                ''', (record_id,))

                row = cursor.fetchone()
                conn.close()

                if row:
                    return {
                        'id': row[0],
                        'timestamp': row[1],
                        'name': row[2],
                        'action': row[3],
                        'duration_hours': row[4],
                        'notes': row[5] or ''
                    }
                return None

            except Exception as e:
                logger.error(f"Error getting record by ID: {e}")
                return None

    def update_record(self, record_id: int, timestamp: str, name: str, action: str, notes: str = '') -> tuple[bool, str]:
        """Update an existing attendance record"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Get the old record first
                cursor.execute('SELECT timestamp, name, action, duration_hours FROM attendance_records WHERE id = ?', (record_id,))
                old_record = cursor.fetchone()
                
                if not old_record:
                    conn.close()
                    return False, "Record not found"

                old_timestamp, old_name, old_action, old_duration = old_record

                # Parse timestamp - handle both ISO format with 'Z' and without
                try:
                    if timestamp.endswith('Z'):
                        # Replace 'Z' with '+00:00' for UTC timezone
                        timestamp = timestamp.replace('Z', '+00:00')
                    # Python 3.7+ supports fromisoformat with timezone
                    parsed_time = datetime.fromisoformat(timestamp)
                    # Convert to local time string without timezone
                    timestamp = parsed_time.replace(tzinfo=None).isoformat()
                except ValueError as e:
                    # Try parsing without timezone
                    try:
                        parsed_time = datetime.strptime(timestamp[:19], '%Y-%m-%dT%H:%M:%S')
                        timestamp = parsed_time.isoformat()
                    except:
                        conn.close()
                        return False, f"Invalid timestamp format: {str(e)}"

                # First, if old record was a check-out with duration, remove it from user hours
                if old_action == 'check-out' and old_duration > 0:
                    cursor.execute('''
                        UPDATE user_hours
                        SET total_hours = total_hours - ?
                        WHERE name = ?
                    ''', (old_duration, old_name))

                # Update the record with initial duration of 0
                cursor.execute('''
                    UPDATE attendance_records
                    SET timestamp = ?, name = ?, action = ?, notes = ?, synced = 0, duration_hours = 0
                    WHERE id = ?
                ''', (timestamp, name, action, notes, record_id))

                # Recalculate duration if this is a check-out record
                new_duration = 0
                if action == 'check-out':
                    # Find the corresponding check-in (most recent one before this checkout)
                    cursor.execute('''
                        SELECT id, timestamp FROM attendance_records
                        WHERE name = ? AND action = 'check-in' AND timestamp < ? AND id != ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    ''', (name, timestamp, record_id))
                    
                    checkin_record = cursor.fetchone()
                    if checkin_record:
                        checkin_time = datetime.fromisoformat(checkin_record[1])
                        checkout_time = datetime.fromisoformat(timestamp)
                        duration_seconds = (checkout_time - checkin_time).total_seconds()
                        new_duration = duration_seconds / 3600

                        if 0 < new_duration < 24:  # Sanity check
                            # Update duration on this record
                            cursor.execute('''
                                UPDATE attendance_records
                                SET duration_hours = ?
                                WHERE id = ?
                            ''', (new_duration, record_id))

                            # Add new duration to user hours
                            cursor.execute('''
                                UPDATE user_hours
                                SET total_hours = total_hours + ?, last_activity = ?
                                WHERE name = ?
                            ''', (new_duration, timestamp[:10], name))
                        else:
                            logger.warning(f"Invalid duration calculated: {new_duration} hours")
                    else:
                        logger.warning(f"No matching check-in found for check-out at {timestamp}")
                
                # If this was a check-in that's now a check-out or vice versa, 
                # we need to recalculate any affected check-outs
                if old_action != action:
                    # Find any check-outs that might have been paired with this record
                    if old_action == 'check-in':
                        # Find check-outs that came after this timestamp
                        cursor.execute('''
                            SELECT id, timestamp FROM attendance_records
                            WHERE name = ? AND action = 'check-out' AND timestamp > ?
                            ORDER BY timestamp ASC
                            LIMIT 1
                        ''', (old_name, old_timestamp))
                        
                        checkout_to_recalc = cursor.fetchone()
                        if checkout_to_recalc:
                            # Recalculate that check-out's duration
                            cursor.execute('''
                                SELECT timestamp FROM attendance_records
                                WHERE name = ? AND action = 'check-in' AND timestamp < ? AND id != ?
                                ORDER BY timestamp DESC
                                LIMIT 1
                            ''', (old_name, checkout_to_recalc[1], checkout_to_recalc[0]))
                            
                            new_checkin = cursor.fetchone()
                            if new_checkin:
                                recalc_duration = (datetime.fromisoformat(checkout_to_recalc[1]) - datetime.fromisoformat(new_checkin[0])).total_seconds() / 3600
                                if 0 < recalc_duration < 24:
                                    # Get old duration for that checkout
                                    cursor.execute('SELECT duration_hours FROM attendance_records WHERE id = ?', (checkout_to_recalc[0],))
                                    old_recalc_duration = cursor.fetchone()[0]
                                    
                                    # Update the checkout duration
                                    cursor.execute('UPDATE attendance_records SET duration_hours = ? WHERE id = ?', (recalc_duration, checkout_to_recalc[0]))
                                    
                                    # Update user hours (remove old, add new)
                                    cursor.execute('''
                                        UPDATE user_hours
                                        SET total_hours = total_hours - ? + ?
                                        WHERE name = ?
                                    ''', (old_recalc_duration, recalc_duration, old_name))

                conn.commit()
                conn.close()
                return True, "Record updated successfully"

            except Exception as e:
                logger.error(f"Error updating record: {e}")
                return False, str(e)

    def delete_record(self, record_id: int) -> tuple[bool, str]:
        """Delete an attendance record and adjust user hours accordingly"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Get the record details before deleting
                cursor.execute('''
                    SELECT name, action, duration_hours
                    FROM attendance_records
                    WHERE id = ?
                ''', (record_id,))

                record = cursor.fetchone()
                if not record:
                    conn.close()
                    return False, "Record not found"

                name, action, duration_hours = record

                # If this was a check-out with hours, subtract from user's total
                if action == 'check-out' and duration_hours > 0:
                    cursor.execute('''
                        UPDATE user_hours
                        SET total_hours = total_hours - ?
                        WHERE name = ?
                    ''', (duration_hours, name))

                # Delete the record
                cursor.execute('DELETE FROM attendance_records WHERE id = ?', (record_id,))

                conn.commit()
                conn.close()
                return True, f"Record deleted successfully"

            except Exception as e:
                logger.error(f"Error deleting record: {e}")
                return False, str(e)

    def get_all_records_with_filters(self, name: str = None, date_from: str = None, date_to: str = None, limit: int = 100) -> List[Dict]:
        """Get attendance records with various filters"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                query = '''
                    SELECT id, timestamp, name, action, duration_hours, notes
                    FROM attendance_records
                    WHERE 1=1
                '''
                params = []

                if name:
                    query += ' AND name = ?'
                    params.append(name)

                if date_from:
                    query += ' AND date(timestamp) >= ?'
                    params.append(date_from)

                if date_to:
                    query += ' AND date(timestamp) <= ?'
                    params.append(date_to)

                query += ' ORDER BY timestamp DESC LIMIT ?'
                params.append(limit)

                cursor.execute(query, params)
                rows = cursor.fetchall()
                conn.close()

                records = []
                for row in rows:
                    records.append({
                        'id': row[0],
                        'timestamp': row[1],
                        'name': row[2],
                        'action': row[3],
                        'duration_hours': row[4] if row[4] is not None else 0,
                        'notes': row[5] or ''
                    })

                return records

            except Exception as e:
                logger.error(f"Error getting filtered records: {e}")
                return []

    def recalculate_missing_durations(self):
        """Recalculate duration for all check-out records that don't have one"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Find all check-out records with no duration or 0 duration
                cursor.execute('''
                    SELECT id, timestamp, name
                    FROM attendance_records
                    WHERE action = 'check-out' AND (duration_hours IS NULL OR duration_hours = 0)
                    ORDER BY timestamp
                ''')
                
                checkouts = cursor.fetchall()
                updated_count = 0

                for checkout_id, checkout_time, name in checkouts:
                    # Find the most recent check-in before this checkout
                    cursor.execute('''
                        SELECT timestamp FROM attendance_records
                        WHERE name = ? AND action = 'check-in' AND timestamp < ? AND id != ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    ''', (name, checkout_time, checkout_id))
                    
                    checkin = cursor.fetchone()
                    if checkin:
                        try:
                            checkin_dt = datetime.fromisoformat(checkin[0])
                            checkout_dt = datetime.fromisoformat(checkout_time)
                            duration = (checkout_dt - checkin_dt).total_seconds() / 3600
                            
                            if 0 < duration < 24:
                                cursor.execute('''
                                    UPDATE attendance_records
                                    SET duration_hours = ?
                                    WHERE id = ?
                                ''', (duration, checkout_id))
                                updated_count += 1
                        except:
                            pass

                conn.commit()
                conn.close()
                
                if updated_count > 0:
                    logger.info(f"✅ Recalculated durations for {updated_count} check-out records")
                
                return updated_count

            except Exception as e:
                logger.error(f"Error recalculating durations: {e}")
                return 0

    def verify_hours_consistency(self) -> bool:
        """Verify that user_hours totals match attendance records and fix if needed"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # Get all users and their total hours from user_hours table
                cursor.execute('SELECT name, total_hours FROM user_hours')
                user_hours_data = {name: hours for name, hours in cursor.fetchall()}
                
                # Calculate expected hours from attendance records
                cursor.execute('''
                    SELECT name, SUM(duration_hours) as calculated_hours
                    FROM attendance_records
                    WHERE (action = 'check-out' AND duration_hours > 0) OR action = 'manual_adjustment'
                    GROUP BY name
                ''')
                calculated_hours = {name: hours for name, hours in cursor.fetchall()}
                
                inconsistencies = []
                
                # Check for inconsistencies
                for name in user_hours_data:
                    stored_hours = user_hours_data[name]
                    calc_hours = calculated_hours.get(name, 0)
                    
                    if abs(stored_hours - calc_hours) > 0.001:  # Allow for small floating point differences
                        inconsistencies.append((name, stored_hours, calc_hours))
                
                # Also check for users in attendance but not in user_hours
                for name in calculated_hours:
                    if name not in user_hours_data and calculated_hours[name] > 0:
                        inconsistencies.append((name, 0, calculated_hours[name]))
                
                if inconsistencies:
                    logger.warning(f"Found {len(inconsistencies)} hours inconsistencies")
                    for name, stored, calculated in inconsistencies:
                        logger.warning(f"  {name}: stored={stored:.3f}h, calculated={calculated:.3f}h")
                    
                    # Auto-fix the inconsistencies
                    logger.info("Auto-fixing hours inconsistencies...")
                    for name, stored, calculated in inconsistencies:
                        cursor.execute('''
                            UPDATE user_hours SET total_hours = ? WHERE name = ?
                        ''', (calculated, name))
                        
                        if cursor.rowcount == 0:
                            # User doesn't exist, create them
                            cursor.execute('''
                                INSERT INTO user_hours (name, total_hours, session_count, last_activity)
                                VALUES (?, ?, 0, date('now'))
                            ''', (name, calculated))
                    
                    conn.commit()
                    logger.info(f"Fixed {len(inconsistencies)} hours inconsistencies")
                
                conn.close()
                return len(inconsistencies) == 0
                
            except Exception as e:
                logger.error(f"Error verifying hours consistency: {e}")
                return False

    def close(self):
        """Ensure all pending transactions are committed and WAL checkpoint is performed"""
        try:
            conn = sqlite3.connect(self.db_path)
            # Perform a full checkpoint to ensure all WAL data is written to main database
            conn.execute('PRAGMA wal_checkpoint(FULL)')
            conn.close()
            logger.info("✅ Database checkpoint completed")
        except Exception as e:
            logger.error(f"❌ Error during database close: {e}")