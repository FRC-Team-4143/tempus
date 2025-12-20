#!/usr/bin/env python3
"""
Demo mode for Attendance Tracker
Runs the application with a mock Google Sheets backend for testing
"""

import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from typing import Dict, List, Optional

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'demo-secret-key-for-testing'

class MockAttendanceTracker:
    """Mock version of AttendanceTracker for demo purposes"""
    
    def __init__(self):
        self.demo_data = []
        self._setup_demo_data()
        logger.info("Demo mode initialized with mock data")
    
    def _setup_demo_data(self):
        """Setup some demo data"""
        base_date = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        
        # Add some sample records for today
        sample_users = [
            ("John Doe", "john.doe@example.com"),
            ("Jane Smith", "jane.smith@example.com"),
            ("Mike Johnson", "mike.johnson@example.com"),
        ]
        
        for i, (name, email) in enumerate(sample_users):
            # Check-in times
            checkin_time = base_date + timedelta(minutes=i*15)
            self.demo_data.append({
                'Timestamp': checkin_time.strftime('%Y-%m-%d %H:%M:%S'),
                'Name': name,
                'Email': email,
                'Action': 'check-in',
                'Date': checkin_time.strftime('%Y-%m-%d'),
                'Check-in Time': checkin_time.strftime('%H:%M:%S'),
                'Check-out Time': '',
                'Duration (hours)': '',
                'Notes': f'Demo check-in for {name}',
                'Device IP': f'192.168.1.{10+i}'
            })
            
            # Some check-outs (but not all)
            if i < 2:  # Only first two users have checked out
                checkout_time = checkin_time + timedelta(hours=8, minutes=30-i*10)
                duration = round((checkout_time - checkin_time).total_seconds() / 3600, 2)
                self.demo_data.append({
                    'Timestamp': checkout_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'Name': name,
                    'Email': email,
                    'Action': 'check-out',
                    'Date': checkout_time.strftime('%Y-%m-%d'),
                    'Check-in Time': '',
                    'Check-out Time': checkout_time.strftime('%H:%M:%S'),
                    'Duration (hours)': duration,
                    'Notes': f'Demo check-out for {name}',
                    'Device IP': f'192.168.1.{10+i}'
                })
    
    def add_attendance_record(self, name: str, email: str, action: str, notes: str = '', device_ip: str = ''):
        """Add an attendance record to the demo data"""
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
            
            row_data = {
                'Timestamp': timestamp,
                'Name': name,
                'Email': email,
                'Action': action,
                'Date': date,
                'Check-in Time': time_str if action == 'check-in' else '',
                'Check-out Time': time_str if action == 'check-out' else '',
                'Duration (hours)': duration,
                'Notes': notes,
                'Device IP': device_ip or '192.168.1.100'
            }
            
            self.demo_data.append(row_data)
            logger.info(f"Demo: Added {action} record for {name}")
            return True, "Record added successfully (Demo Mode)"
            
        except Exception as e:
            logger.error(f"Demo error adding record: {e}")
            return False, f"Error adding record: {e}"
    
    def get_user_records_today(self, email: str) -> List[Dict]:
        """Get user's records for today from demo data"""
        today = datetime.now().strftime('%Y-%m-%d')
        user_records_today = [
            record for record in self.demo_data
            if record.get('Email') == email and record.get('Date') == today
        ]
        return user_records_today
    
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
        """Get all demo attendance records"""
        # Return latest records first
        return self.demo_data[-limit:][::-1]
    
    def get_daily_summary(self, date: str = None) -> Dict:
        """Get daily attendance summary from demo data"""
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        daily_records = [
            record for record in self.demo_data
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

# Initialize the demo tracker
tracker = MockAttendanceTracker()

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

if __name__ == '__main__':
    host = '0.0.0.0'
    port = 5000
    
    print("\n" + "="*50)
    print("🎭 ATTENDANCE TRACKER - DEMO MODE")
    print("="*50)
    print("📝 This is a demonstration version with mock data")
    print("💾 No real Google Sheets integration")
    print("🔄 Data resets when application restarts")
    print(f"🌐 Access at: http://localhost:{port}")
    print(f"📱 Network access: http://YOUR_IP:{port}")
    print("="*50)
    print()
    
    logger.info(f"Starting demo attendance tracker on {host}:{port}")
    app.run(host=host, port=port, debug=True)