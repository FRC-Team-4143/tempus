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

# Database lock to prevent race conditions between user operations and background sync
db_lock = Lock()

# Configure logging
logger = logging.getLogger(__name__)

class LocalDatabase:
    def __init__(self):
        # Use absolute path to data directory
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        self.db_path = os.path.join(data_dir, 'attendance.db')
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
                    INSERT INTO attendance_records (timestamp, name, action, duration_hours)
                    VALUES (?, ?, ?, ?)
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
                for row in rows:
                    leaderboard.append({
                        'name': row[0],
                        'total_hours': round(row[1], 2),
                        'sessions': row[2],
                        'last_activity': row[3] or 'Never'
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

    def adjust_user_hours(self, name: str, adjustment_hours: float) -> tuple[bool, str]:
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
                cursor.execute('UPDATE user_hours SET total_hours = ? WHERE name = ?', (new_total, name))

                conn.commit()
                conn.close()

                return True, f"Successfully adjusted {name}'s hours"

            except Exception as e:
                logger.error(f"Error adjusting user hours: {e}")
                return False, str(e)