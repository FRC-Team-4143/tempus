#!/usr/bin/env python3
"""
Utility functions for Attendance Tracking System
"""

import os
import logging
import sys
from typing import Dict, List
from datetime import datetime, timedelta

# Add the app directory to the path so we can import database
sys.path.append(os.path.dirname(__file__))
from database import LocalDatabase

logger = logging.getLogger(__name__)

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
    """Load names from users.csv file with team numbers and group by team"""
    global PRESET_NAMES
    try:
        # Load from users.csv and group by team
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        users_path = os.path.join(data_dir, 'users.csv')
        if os.path.exists(users_path):
            with open(users_path, 'r', encoding='utf-8') as f:
                team_4143_names = []
                team_4423_names = []

                for line in f:
                    line = line.strip()
                    if line:
                        # Parse CSV line: "Name","TeamNumber","Category","SlackUID"
                        parts = [part.strip('"') for part in line.split(',')]
                        if len(parts) >= 2:
                            name = parts[0]
                            team = parts[1]

                            if team == '4143':
                                team_4143_names.append(name)
                            elif team == '4423':
                                team_4423_names.append(name)

                # Sort names within each team
                team_4143_names.sort()
                team_4423_names.sort()

                # Combine with team headers
                grouped_names = []
                if team_4143_names:
                    grouped_names.append('--- Team 4143 ---')
                    grouped_names.extend(team_4143_names)
                if team_4423_names:
                    grouped_names.append('--- Team 4423 ---')
                    grouped_names.extend(team_4423_names)

                if grouped_names:
                    PRESET_NAMES[:] = grouped_names  # Modify in place to preserve references
                    logger.info(f'Loaded {len(team_4143_names)} Team 4143 names and {len(team_4423_names)} Team 4423 names from users.csv')
                    
                    # Clean up old users from database
                    db = LocalDatabase()
                    db.cleanup_old_users(PRESET_NAMES)
    except Exception as e:
        logger.warning(f'Could not load names from file: {e}')

def get_team_roster_mapping():
    """Get a mapping of names to team numbers from users.csv"""
    team_mapping = {}
    try:
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        users_path = os.path.join(data_dir, 'users.csv')
        if os.path.exists(users_path):
            with open(users_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        parts = [part.strip('"') for part in line.split(',')]
                        if len(parts) >= 2:
                            name = parts[0]
                            team = parts[1]
                            # Skip category field (parts[2]) for now
                            team_mapping[name] = team
    except Exception as e:
        logger.warning(f'Could not load team mapping: {e}')
    return team_mapping

def get_category_mapping():
    """Get a mapping of names to categories from users.csv"""
    category_mapping = {}
    try:
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        users_path = os.path.join(data_dir, 'users.csv')
        if os.path.exists(users_path):
            with open(users_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        parts = [part.strip('"') for part in line.split(',')]
                        if len(parts) >= 3:
                            name = parts[0]
                            category = parts[2]
                            if category:  # Only add if category is not empty
                                category_mapping[name] = category
    except Exception as e:
        logger.warning(f'Could not load category mapping: {e}')
    return category_mapping

def get_slack_uid_mapping():
    """Get a mapping of names to Slack UIDs from users.csv"""
    slack_mapping = {}
    try:
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        users_path = os.path.join(data_dir, 'users.csv')
        if os.path.exists(users_path):
            with open(users_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        parts = [part.strip('"') for part in line.split(',')]
                        if len(parts) >= 4:
                            name = parts[0]
                            slack_uid = parts[3]
                            if slack_uid:  # Only add if slack UID is not empty
                                slack_mapping[name] = slack_uid
    except Exception as e:
        logger.warning(f'Could not load Slack UID mapping: {e}')
    return slack_mapping

def calculate_total_expected_hours(current_date: datetime = None) -> float:
    """Calculate total expected hours based on configurable dates and weekly increase
    
    Args:
        current_date: Date to calculate for (defaults to today)
    
    Returns:
        Total expected hours accumulated up to the current date
    """
    if current_date is None:
        current_date = datetime.now()
    
    # Load environment variables from config/.env
    from dotenv import load_dotenv
    import os
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config', '.env')
    load_dotenv(config_dir)
    
    # Get configuration from environment
    start_date_str = os.environ.get('EXPECTED_HOURS_START_DATE', '2024-01-01')
    end_date_str = os.environ.get('EXPECTED_HOURS_END_DATE', '2024-12-31')
    weekly_increase = float(os.environ.get('EXPECTED_HOURS_WEEKLY_INCREASE', '11'))
    
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
    except ValueError as e:
        logger.error(f"Invalid date format in configuration: {e}")
        return 0.0
    
    # If current date is before start date, return 0
    if current_date < start_date:
        return 0.0
    
    # If current date is after end date, calculate up to end date
    calculation_date = min(current_date, end_date)
    
    # Calculate number of weeks from start to calculation date
    weeks_elapsed = (calculation_date - start_date).days // 7
    
    # If we're on or after the start date, we should have at least 1 week
    if weeks_elapsed < 0:
        return 0.0
    elif weeks_elapsed == 0:
        # First week: just the base weekly hours
        return weekly_increase
    
    # For subsequent weeks: sum of arithmetic series
    # Total expected hours = sum of arithmetic series: n/2 * (first + last)
    # where first = weekly_increase, last = weekly_increase * weeks_elapsed
    total_expected = (weeks_elapsed / 2) * (weekly_increase + (weekly_increase * weeks_elapsed))
    
    return round(total_expected, 2)

def get_expected_hours_config():
    """Get expected hours configuration from environment"""
    from dotenv import load_dotenv
    import os
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config', '.env')
    load_dotenv(config_dir)
    
    start_date_str = os.environ.get('EXPECTED_HOURS_START_DATE', '2024-01-01')
    end_date_str = os.environ.get('EXPECTED_HOURS_END_DATE', '2024-12-31')
    weekly_increase = float(os.environ.get('EXPECTED_HOURS_WEEKLY_INCREASE', '11'))
    
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
    except ValueError:
        start_date = datetime(2024, 1, 1)
        end_date = datetime(2024, 12, 31)
    
    return {
        'start_date': start_date,
        'end_date': end_date,
        'weekly_increase': weekly_increase
    }

def calculate_week_number(target_date: datetime) -> int:
    """Calculate the week number from the expected hours start date
    
    Args:
        target_date: Date to calculate week number for
    
    Returns:
        Week number (1-based) from the start date
    """
    config = get_expected_hours_config()
    start_date = config['start_date']
    
    if target_date < start_date:
        return 1
    
    # Calculate week number (1-based)
    weeks_elapsed = ((target_date - start_date).days // 7) + 1
    return max(1, weeks_elapsed)