#!/usr/bin/env python3
"""
Utility functions for Attendance Tracking System
"""

import os
import logging
import sys
from typing import Dict, List

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
                        # Parse CSV line: "Name","TeamNumber"
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
                    PRESET_NAMES = grouped_names
                    logger.info(f'Loaded {len(team_4143_names)} Team 4143 names and {len(team_4423_names)} Team 4423 names from users.csv')
                    
                    # Clean up old users from database
                    db = LocalDatabase()
                    db.cleanup_old_users(PRESET_NAMES)
        elif os.path.exists(os.path.join(data_dir, 'names_list.csv')):
            # Fallback to simple names list if users.csv doesn't exist
            with open(os.path.join(data_dir, 'names_list.csv'), 'r', encoding='utf-8') as f:
                file_names = []
                for line in f:
                    name = line.strip().strip('"')
                    if name:
                        file_names.append(name)
                if file_names:
                    PRESET_NAMES = file_names
                    logger.info(f'Loaded {len(file_names)} names from names_list.csv')
                    
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

# Load custom names from files on module import
load_names_from_file()