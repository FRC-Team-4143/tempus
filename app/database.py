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
                    INSERT INTO attendance_records (timestamp, name, action, duration_hours, notes)
                    VALUES (?, ?, ?, ?, NULL)
                ''', (timestamp, name, action, duration_hours))

                conn.commit()
                conn.close()
                return True

            except Exception as e:
                logger.error(f"Error adding record: {e}")
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

    def adjust_user_hours(self, name: str, adjustment_hours: float, adjustment_date: str = None, reason: str = None) -> tuple[bool, str]:
        """Adjust a user's total hours"""
        with db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()

                # Get current total
                cursor.execute('SELECT total_hours FROM user_hours WHERE name = ?', (name,))
                result = cursor.fetchone()

                if not result:
                    return False, f"User {name} not found"

                current_total = result[0]
                new_total = current_total + adjustment_hours

                # Update the total
                cursor.execute('UPDATE user_hours SET total_hours = ?, last_activity = ? WHERE name = ?', 
                             (new_total, datetime.now().isoformat(), name))

                # Create an attendance record for the manual adjustment to include it in weekly calculations
                if adjustment_date:
                    # Use the specified date with current time
                    timestamp = f"{adjustment_date}T{datetime.now().strftime('%H:%M:%S.%f')}"
                else:
                    # Use current timestamp
                    timestamp = datetime.now().isoformat()
                    
                cursor.execute('''
                    INSERT INTO attendance_records (timestamp, name, action, duration_hours, notes)
                    VALUES (?, ?, ?, ?, ?)
                ''', (timestamp, name, 'manual_adjustment', adjustment_hours, reason))

                conn.commit()
                conn.close()

                return True, f"Successfully adjusted {name}'s hours"

            except Exception as e:
                logger.error(f"Error adjusting user hours: {e}")
                return False, str(e)

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