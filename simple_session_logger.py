"""
Simple Session Logger - Production Grade
Captures ALL logs from websocket open to close, no complex context verification needed
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from collections import deque
import queue

from aws_client_manager import get_s3_client
from constants import USER_SESSION_LOGGING_CONFIG


class SimpleSessionLogger:
    """
    Simple, production-grade session logger that captures ALL logs during a session
    """
    
    def __init__(self):
        self.s3_bucket = USER_SESSION_LOGGING_CONFIG["s3_bucket"]
        self.enabled = USER_SESSION_LOGGING_CONFIG["enabled"]
        
        # Simple session tracking
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.session_logs: Dict[str, List[str]] = {}
        
        # Thread safety
        self._lock = threading.RLock()
        
        # S3 client
        self._s3_client = None
        self._s3_client_lock = threading.Lock()
        
        # Statistics
        self._total_sessions_created = 0
        self._total_sessions_closed = 0
        self._total_s3_uploads = 0
        
        logging.info(f"SimpleSessionLogger initialized - S3 bucket: {self.s3_bucket}, enabled: {self.enabled}")
    
    def _get_session_key(self, user_id: str, session_id: str) -> str:
        """Generate a unique session key"""
        return f"{user_id}:{session_id}"
    
    def start_session(self, user_id: str, session_id: str, websocket_id: str = None):
        """Start logging session - captures ALL logs from this point"""
        if not self.enabled:
            return
            
        session_key = self._get_session_key(user_id, session_id)
        timestamp = datetime.now(timezone.utc).isoformat()
        
        with self._lock:
            # Initialize session
            self.active_sessions[session_key] = {
                'user_id': user_id,
                'session_id': session_id,
                'websocket_id': websocket_id,
                'start_time': timestamp,
                'log_count': 0
            }
            
            # Initialize log list
            self.session_logs[session_key] = []
            self._total_sessions_created += 1
            
        logging.info(f"Started session logging for user {user_id}, session {session_id}")
    
    def stop_session(self, user_id: str, session_id: str, websocket_id: str = None):
        """Stop logging session and save to S3"""
        if not self.enabled:
            return
            
        session_key = self._get_session_key(user_id, session_id)
        timestamp = datetime.now(timezone.utc).isoformat()
        
        with self._lock:
            if session_key not in self.session_logs:
                logging.warning(f"Session {session_key} not found for closure")
                return
            
    
            # Get logs and session info
            logs = self.session_logs[session_key].copy()
            session_info = self.active_sessions.get(session_key, {})
            
            # Clean up
            self.active_sessions.pop(session_key, None)
            self.session_logs.pop(session_key, None)
            self._total_sessions_closed += 1
            
        # Save to S3 asynchronously
        if logs:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._save_to_s3(session_key, user_id, session_id, session_info, logs))
            except RuntimeError:
                # No event loop, save synchronously
                asyncio.run(self._save_to_s3(session_key, user_id, session_id, session_info, logs))
        
        logging.info(f"Stopped session logging for user {user_id}, session {session_id}")
    
    def log_message(self, message: str, level: str = "INFO"):
        """Log a message to all active sessions - NO DUPLICATE PREVENTION"""
        if not self.enabled:
            return
            
        # Use exact same format as console output: "LEVEL:logger_name:message"
        log_line = f"{level}:root:{message}"
        
        with self._lock:
            # Only log if we have active sessions
            if not self.active_sessions:
                return
                
            for session_key in self.active_sessions.keys():
                if session_key in self.session_logs:
                    # NO DUPLICATE PREVENTION - capture everything exactly as it appears
                    self.session_logs[session_key].append(log_line)
                    self.active_sessions[session_key]['log_count'] += 1
    
    async def _save_to_s3(self, session_key: str, user_id: str, session_id: str, session_info: Dict[str, Any], logs: List[str]):
        """Save session logs to S3"""
        try:
            # Create S3 key
            s3_key = f"{user_id}/{session_id}.txt"
            
            # Create content with header
            content = self._format_logs_for_s3(user_id, session_id, session_info, logs)
            
            # Get S3 client
            s3_client = await self._get_s3_client_async()
            
            # Upload to S3
            await asyncio.wait_for(
                asyncio.to_thread(
                    s3_client.put_object,
                    Bucket=self.s3_bucket,
                    Key=s3_key,
                    Body=content.encode('utf-8'),
                    ContentType='text/plain; charset=utf-8',
                    ServerSideEncryption='AES256',
                    Metadata={
                        'session-id': session_id,
                        'user-id': user_id,
                        'upload-time': datetime.now(timezone.utc).isoformat(),
                        'log-count': str(len(logs))
                    }
                ),
                timeout=30.0
            )
            
            self._total_s3_uploads += 1
            logging.info(f"✅ Saved {len(logs)} logs to S3: s3://{self.s3_bucket}/{s3_key}")
            
        except Exception as e:
            logging.error(f"❌ Failed to save session logs to S3: {str(e)}")
    
    def _format_logs_for_s3(self, user_id: str, session_id: str, session_info: Dict[str, Any], logs: List[str]) -> str:
        """Format logs for S3 storage - exact same format as console output"""
        # Return logs in exact same format as console - no headers, just raw logs
        return "\n".join(logs)
    
    async def _get_s3_client_async(self):
        """Get S3 client with async initialization"""
        if self._s3_client is None:
            with self._s3_client_lock:
                if self._s3_client is None:
                    self._s3_client = await asyncio.to_thread(get_s3_client)
        
        return self._s3_client
    
    def get_stats(self) -> Dict[str, Any]:
        """Get logger statistics"""
        with self._lock:
            return {
                "active_sessions": len(self.active_sessions),
                "total_logs_buffered": sum(len(logs) for logs in self.session_logs.values()),
                "total_sessions_created": self._total_sessions_created,
                "total_sessions_closed": self._total_sessions_closed,
                "total_s3_uploads": self._total_s3_uploads,
                "enabled": self.enabled
            }


# Global instance
simple_session_logger = SimpleSessionLogger()

# Simple integration functions
def start_session_logging(user_id: str, session_id: str, websocket_id: str = None):
    """Start session logging"""
    simple_session_logger.start_session(user_id, session_id, websocket_id)

def stop_session_logging(user_id: str, session_id: str, websocket_id: str = None):
    """Stop session logging"""
    simple_session_logger.stop_session(user_id, session_id, websocket_id)

def log_to_session(message: str, level: str = "INFO"):
    """Log a message to all active sessions"""
    simple_session_logger.log_message(message, level)
