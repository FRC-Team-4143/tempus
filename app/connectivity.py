#!/usr/bin/env python3
"""
Network connectivity utilities for Attendance Tracking System
Checks internet connectivity before attempting internet-dependent operations
"""

import socket
import logging
from typing import Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Cache connectivity status to avoid excessive checks
_last_check_time: Optional[datetime] = None
_last_check_result: bool = False
_check_cache_duration = timedelta(seconds=30)  # Cache for 30 seconds


def check_internet_connection(timeout: float = 3.0) -> bool:
    """
    Check if internet connection is available
    
    Args:
        timeout: Connection timeout in seconds
        
    Returns:
        True if internet is available, False otherwise
    """
    global _last_check_time, _last_check_result
    
    # Return cached result if recent
    if _last_check_time and datetime.now() - _last_check_time < _check_cache_duration:
        return _last_check_result
    
    # Try to connect to multiple reliable hosts
    test_hosts = [
        ('8.8.8.8', 53),       # Google DNS
        ('1.1.1.1', 53),       # Cloudflare DNS
        ('208.67.222.222', 53) # OpenDNS
    ]
    
    for host, port in test_hosts:
        try:
            socket.setdefaulttimeout(timeout)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            sock.close()
            
            # Update cache
            _last_check_time = datetime.now()
            _last_check_result = True
            return True
            
        except (socket.error, socket.timeout):
            continue
    
    # Update cache
    _last_check_time = datetime.now()
    _last_check_result = False
    return False


def check_google_sheets_connection() -> bool:
    """
    Check if Google Sheets API is accessible
    
    Returns:
        True if Google Sheets is accessible, False otherwise
    """
    if not check_internet_connection():
        return False
    
    try:
        # Try to resolve Google Sheets API hostname
        socket.gethostbyname('sheets.googleapis.com')
        return True
    except socket.error:
        logger.warning("Cannot reach Google Sheets API")
        return False


def check_slack_connection() -> bool:
    """
    Check if Slack API is accessible
    
    Returns:
        True if Slack is accessible, False otherwise
    """
    if not check_internet_connection():
        return False
    
    try:
        # Try to resolve Slack API hostname
        socket.gethostbyname('slack.com')
        return True
    except socket.error:
        logger.warning("Cannot reach Slack API")
        return False


def invalidate_cache():
    """Force next connectivity check to be fresh (useful after network changes)"""
    global _last_check_time
    _last_check_time = None
