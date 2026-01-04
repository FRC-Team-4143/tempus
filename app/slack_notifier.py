#!/usr/bin/env python3
"""
Slack notification module for Attendance Tracking System
Sends notifications to users who don't meet weekly attendance requirements
"""

import os
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

from .database import LocalDatabase
from .utils import get_team_roster_mapping, get_category_mapping
from .connectivity import check_slack_connection

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Handles Slack notifications for attendance tracking"""
    
    def __init__(self):
        """Initialize Slack client"""
        self.enabled = os.environ.get('SLACK_ENABLED', 'False').lower() == 'true'
        self.bot_token = os.environ.get('SLACK_BOT_TOKEN', '')
        
        if self.enabled and self.bot_token:
            try:
                self.client = WebClient(token=self.bot_token)
                # Test the connection
                auth_response = self.client.auth_test()
                logger.info(f"✅ Slack bot connected as {auth_response['user']}")
            except SlackApiError as e:
                logger.error(f"❌ Failed to connect to Slack: {e}")
                self.enabled = False
                self.client = None
        else:
            self.client = None
            if not self.enabled:
                logger.info("Slack notifications disabled in configuration")
            else:
                logger.warning("Slack bot token not configured")
    
    def load_slack_user_mapping(self) -> Dict[str, str]:
        """Load mapping of names to Slack user IDs from data/users.csv
        
        Returns:
            Dictionary mapping full names to Slack user IDs
        """
        mapping = {}
        try:
            users_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'users.csv')
            if os.path.exists(users_path):
                with open(users_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            # Parse CSV: "Name","TeamNumber","Category","SlackUserID"
                            parts = [part.strip('"') for part in line.split(',')]
                            if len(parts) >= 4:
                                name = parts[0]
                                slack_id = parts[3]
                                if slack_id:  # Only add if Slack ID is provided
                                    mapping[name] = slack_id
            else:
                logger.warning("data/users.csv not found")
        except Exception as e:
            logger.error(f"Error loading Slack user mapping: {e}")
        
        return mapping
    
    def load_mentor_mapping(self) -> tuple[Dict[tuple, str], str]:
        """Load mapping of (team, category) to mentor Slack IDs from mentors.csv
        
        Returns:
            Tuple of (mapping dict, lead_mentor_id)
        """
        mapping = {}
        lead_mentor_id = None
        try:
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
                                slack_id = parts[3]
                                if team == 'LEAD' and category == 'ALL':
                                    lead_mentor_id = slack_id if slack_id else None
                                elif slack_id:  # Only add if Slack ID is provided
                                    mapping[(team, category)] = slack_id
            else:
                logger.warning("mentors.csv not found")
        except Exception as e:
            logger.error(f"Error loading mentor mapping: {e}")
        
        return mapping, lead_mentor_id
    
    def send_dm(self, user_id: str, message: str) -> bool:
        """Send a direct message to a Slack user
        
        Args:
            user_id: Slack user ID (e.g., U01234567)
            message: Message text to send
            
        Returns:
            True if message sent successfully, False otherwise
        """
        if not self.enabled or not self.client:
            logger.warning("Slack notifications not enabled or client not initialized")
            return False
        
        # Check internet connectivity
        if not check_slack_connection():
            logger.warning("⚠️ No internet connection, cannot send Slack DM")
            return False
        
        try:
            # Open a DM channel with the user
            response = self.client.conversations_open(users=[user_id])
            channel_id = response['channel']['id']
            
            # Send the message
            self.client.chat_postMessage(
                channel=channel_id,
                text=message,
                mrkdwn=True
            )
            
            logger.info(f"✅ Sent Slack DM to user {user_id}")
            return True
            
        except SlackApiError as e:
            logger.error(f"❌ Failed to send Slack DM to {user_id}: {e}")
            return False
    
    def send_group_dm(self, user_ids: List[str], message: str) -> bool:
        """Send a direct message to multiple Slack users (group DM)
        
        Args:
            user_ids: List of Slack user IDs (e.g., [U01234567, U89012345])
            message: Message text to send
            
        Returns:
            True if message sent successfully, False otherwise
        """
        if not self.enabled or not self.client:
            logger.warning("Slack notifications not enabled or client not initialized")
            return False
        
        # Check internet connectivity
        if not check_slack_connection():
            logger.warning("⚠️ No internet connection, cannot send Slack group DM")
            return False
        
        try:
            # Open a multi-party DM channel
            response = self.client.conversations_open(users=user_ids)
            channel_id = response['channel']['id']
            
            # Send the message
            self.client.chat_postMessage(
                channel=channel_id,
                text=message,
                mrkdwn=True
            )
            
            logger.info(f"✅ Sent Slack group DM to users {user_ids}")
            return True
            
        except SlackApiError as e:
            logger.error(f"❌ Failed to send Slack group DM to {user_ids}: {e}")
            return False
    
    def check_and_notify_weekly_attendance(self, weeks_back: int = 0) -> Dict[str, any]:
        """Send weekly attendance summaries to all students
        
        Args:
            weeks_back: Number of weeks back to check (0 = current week)
            
        Returns:
            Dictionary with notification results
        """
        if not self.enabled or not self.client:
            return {
                'success': False,
                'message': 'Slack notifications not enabled',
                'notified_users': []
            }
        
        # Check internet connectivity
        if not check_slack_connection():
            logger.warning("⚠️ No internet connection, cannot send Slack notifications")
            return {
                'success': False,
                'message': 'No internet connection available',
                'notified_users': [],
                'offline': True
            }
        
        try:
            # Get weekly attendance data
            db = LocalDatabase()
            weekly_data = db.get_weekly_attendance(weeks_back)
            
            # Load Slack user mapping
            slack_mapping = self.load_slack_user_mapping()
            mentor_mapping, lead_mentor_id = self.load_mentor_mapping()
            
            # Get team and category mappings
            team_mapping = get_team_roster_mapping()
            category_mapping = get_category_mapping()
            
            notified_users = []
            failed_users = []
            skipped_users = []
            
            # Send weekly summary to ALL users
            for name, data in weekly_data.items():
                # Check if we have a Slack ID for this user
                slack_id = slack_mapping.get(name)
                
                if not slack_id:
                    skipped_users.append(name)
                    logger.warning(f"No Slack ID found for {name}")
                    continue
                
                # Get student's team and category
                team = team_mapping.get(name, '')
                category = category_mapping.get(name, '')
                
                # Check if student is below 80% on season totals
                season_status = data.get('total_status', 'good')
                needs_mentor = season_status in ['warning', 'danger']
                
                # Format the notification message
                message = self._format_attendance_message(name, data, team_mapping, category_mapping)
                
                # If student needs intervention, include mentor(s) in group DM
                if needs_mentor and team and category:
                    mentor_id = mentor_mapping.get((team, category))
                    
                    # Build list of recipients: student + category mentor + lead mentor
                    recipients = [slack_id]
                    if mentor_id:
                        recipients.append(mentor_id)
                    if lead_mentor_id and lead_mentor_id not in recipients:
                        recipients.append(lead_mentor_id)
                    
                    if len(recipients) > 1:  # At least student + one mentor
                        # Send as group DM with mentor(s) - use condensed mentor alert format
                        mentor_message = self._format_attendance_message(name, data, team_mapping, category_mapping, mentor_alert=True)
                        if self.send_group_dm(recipients, mentor_message):
                            notified_users.append(name)
                            logger.info(f"Sent group DM to {name} with {len(recipients)-1} mentor(s)")
                        else:
                            failed_users.append(name)
                    else:
                        # No mentor configured, send regular DM
                        if self.send_dm(slack_id, message):
                            notified_users.append(name)
                        else:
                            failed_users.append(name)
                else:
                    # Student is doing well, send regular DM
                    if self.send_dm(slack_id, message):
                        notified_users.append(name)
                    else:
                        failed_users.append(name)
            
            result = {
                'success': True,
                'message': f'Notified {len(notified_users)} users',
                'notified_users': notified_users,
                'failed_users': failed_users,
                'skipped_users': skipped_users,
                'week_start': weekly_data[list(weekly_data.keys())[0]]['week_start'] if weekly_data else None,
                'week_end': weekly_data[list(weekly_data.keys())[0]]['week_end'] if weekly_data else None,
            }
            
            logger.info(f"Weekly attendance notifications complete: {len(notified_users)} notified, "
                       f"{len(failed_users)} failed, {len(skipped_users)} skipped")
            
            return result
            
        except Exception as e:
            logger.error(f"Error checking weekly attendance: {e}")
            return {
                'success': False,
                'message': f'Error: {str(e)}',
                'notified_users': []
            }
    
    def _format_attendance_message(self, name: str, data: Dict, 
                                   team_mapping: Dict, category_mapping: Dict,
                                   mentor_alert: bool = False) -> str:
        """Format the attendance notification message
        
        Args:
            name: User's name
            data: Weekly attendance data
            team_mapping: Mapping of names to team numbers
            category_mapping: Mapping of names to categories
            mentor_alert: Whether this is a mentor alert message
            
        Returns:
            Formatted message string
        """
        status_text = {
            'good': '✅ Meeting Requirements',
            'warning': '⚠️ Below Target',
            'danger': '❌ Action Needed'
        }
        
        weekly_status = status_text.get(data['status'], 'Status Unknown')
        season_status = status_text.get(data.get('total_status', 'good'), '✅ Meeting Requirements')
        season_status_key = data.get('total_status', 'good')
        
        # Mentor alert header if needed
        if mentor_alert:
            message = f"""
🔔 *Mentor Alerted*

Due to current attendance records a mentor has been included on this weeks summary.

"""
        else:
            message = ""
        
        # Basic attendance summary for everyone
        message += f"""
📊 *Weekly Attendance Summary*

Hi {name}!

Week of *{data['week_start']} to {data['week_end']}*
• Weekly: *{data['total_hours']} / {data['required_hours']} hrs* ({data['attendance_percentage']}%) - {weekly_status}
• Season: *{data['all_time_hours']} / {data['total_expected_hours']} hrs* ({data['total_hours_ratio']}%) - {season_status}
"""
        
        # Add guidance if below target
        if season_status_key == 'danger':
            message += "\n⚠️ Please work with your mentor to develop a catch-up plan.\n"
        elif season_status_key == 'warning':
            message += "\n⚠️ Try to complete more hours each week to get back on track.\n"
        elif season_status_key == 'good':
            message += "\n🎉 Keep up the great work!\n"
        
        return message
    
    def send_test_notification(self, user_name: str) -> Dict[str, any]:
        """Send a test notification to a specific user
        
        Args:
            user_name: Name of the user to send test to
            
        Returns:
            Dictionary with test result
        """
        if not self.enabled or not self.client:
            return {
                'success': False,
                'message': 'Slack notifications not enabled'
            }
        
        try:
            # Load Slack user mapping
            slack_mapping = self.load_slack_user_mapping()
            
            slack_id = slack_mapping.get(user_name)
            if not slack_id:
                return {
                    'success': False,
                    'message': f'No Slack ID found for {user_name}'
                }
            
            # Send test message
            message = f"""
🧪 *Test Notification*

Hi {user_name}!

This is a test notification from the FRC Attendance Tracking System. If you're receiving this, the Slack integration is working correctly! ✅

You'll receive automatic notifications each Sunday evening if you don't meet the weekly attendance requirement.
"""
            
            if self.send_dm(slack_id, message):
                return {
                    'success': True,
                    'message': f'Test notification sent to {user_name}'
                }
            else:
                return {
                    'success': False,
                    'message': f'Failed to send test notification to {user_name}'
                }
                
        except Exception as e:
            logger.error(f"Error sending test notification: {e}")
            return {
                'success': False,
                'message': f'Error: {str(e)}'
            }
