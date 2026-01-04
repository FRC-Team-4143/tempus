#!/usr/bin/env python3
"""
Scheduler module for Attendance Tracking System
Manages scheduled tasks like weekly Slack notifications
"""

import os
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from .slack_notifier import SlackNotifier
from .connectivity import check_slack_connection

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

logger = logging.getLogger(__name__)


class AttendanceScheduler:
    """Manages scheduled tasks for the attendance system"""
    
    def __init__(self):
        """Initialize the scheduler"""
        self.scheduler = BackgroundScheduler()
        self.slack_notifier = SlackNotifier()
        
        # Get configuration from environment
        self.notification_day = int(os.environ.get('SLACK_NOTIFICATION_DAY', '6'))  # Default: Sunday
        self.notification_hour = int(os.environ.get('SLACK_NOTIFICATION_HOUR', '20'))  # Default: 8 PM
        
    def start(self):
        """Start the scheduler and add jobs"""
        if not self.slack_notifier.enabled:
            logger.info("Slack notifications disabled, skipping scheduler setup")
            return
        
        try:
            # Schedule weekly attendance check
            # Run every week on the configured day at the configured hour
            self.scheduler.add_job(
                func=self._weekly_attendance_check,
                trigger=CronTrigger(
                    day_of_week=self.notification_day,
                    hour=self.notification_hour,
                    minute=0
                ),
                id='weekly_attendance_check',
                name='Weekly Attendance Check and Notification',
                replace_existing=True
            )
            
            self.scheduler.start()
            
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            day_name = day_names[self.notification_day]
            
            logger.info(f"✅ Scheduler started - Weekly notifications scheduled for {day_name}s at {self.notification_hour}:00")
            
        except Exception as e:
            logger.error(f"❌ Failed to start scheduler: {e}")
    
    def stop(self):
        """Stop the scheduler"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")
    
    def _weekly_attendance_check(self):
        """Scheduled job to check weekly attendance and send notifications"""
        try:
            # Check internet connectivity before attempting to send notifications
            if not check_slack_connection():
                logger.warning("⚠️ No internet connection, skipping scheduled weekly notifications")
                return
            
            logger.info("Running scheduled weekly attendance check...")
            result = self.slack_notifier.check_and_notify_weekly_attendance()
            
            if result['success']:
                logger.info(f"✅ Weekly notifications sent: {result['message']}")
                logger.info(f"   Notified users: {result.get('notified_users', [])}")
                logger.info(f"   Failed users: {result.get('failed_users', [])}")
                logger.info(f"   Skipped users: {result.get('skipped_users', [])}")
            else:
                if result.get('offline'):
                    logger.warning(f"⚠️ Weekly notifications skipped: {result['message']}")
                else:
                    logger.error(f"❌ Weekly notification check failed: {result['message']}")
                
        except Exception as e:
            logger.error(f"❌ Error in scheduled weekly attendance check: {e}")
    
    def run_now(self):
        """Manually trigger the weekly attendance check (for testing)"""
        logger.info("Manually triggering weekly attendance check...")
        self._weekly_attendance_check()


# Global scheduler instance
_scheduler_instance = None


def get_scheduler() -> AttendanceScheduler:
    """Get the global scheduler instance"""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = AttendanceScheduler()
    return _scheduler_instance


def start_scheduler():
    """Start the global scheduler"""
    scheduler = get_scheduler()
    scheduler.start()


def stop_scheduler():
    """Stop the global scheduler"""
    scheduler = get_scheduler()
    scheduler.stop()
