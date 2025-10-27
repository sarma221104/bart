"""
Simple Log Handler - Production Grade
Captures ALL logs during active sessions, no complex context verification needed
"""

import logging
import threading
from typing import Optional

from simple_session_logger import simple_session_logger, start_session_logging, stop_session_logging


class SimpleLogHandler(logging.Handler):
    """
    Simple logging handler that captures ALL logs during active sessions
    """
    
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self._lock = threading.RLock()
        self._total_logs_processed = 0
    
    def emit(self, record):
        """Emit a log record to active sessions - NO FILTERING, CAPTURE EVERYTHING"""
        try:
            message = record.getMessage()
            level = record.levelname
            
            # Skip only our own logging to prevent infinite loops
            if 'SimpleLogHandler' in message or 'SimpleSessionLogger' in message:
                return
            
            with self._lock:
                self._total_logs_processed += 1
            
            # Add exception info if present
            if record.exc_info:
                import traceback
                exc_text = traceback.format_exception(*record.exc_info)
                message += f"\nException: {''.join(exc_text)}"
            
            # Log to all active sessions - capture EVERYTHING
            if simple_session_logger.active_sessions:
                simple_session_logger.log_message(message, level)
            
        except Exception as e:
            # Fallback to prevent logging loops
            print(f"❌ Error in SimpleLogHandler: {str(e)}")
    
    def get_stats(self) -> dict:
        """Get handler statistics"""
        with self._lock:
            return {
                "total_logs_processed": self._total_logs_processed,
                "active_sessions": len(simple_session_logger.active_sessions)
            }


def setup_simple_logging():
    """Setup simple logging configuration"""
    print("🚀 Setting up simple session logging...")
    
    # Get root logger
    root_logger = logging.getLogger()
    
    # Check if handler already exists
    for handler in root_logger.handlers:
        if isinstance(handler, SimpleLogHandler):
            print("SimpleLogHandler already exists, skipping setup")
            return
    
    # Create and add handler
    log_handler = SimpleLogHandler(level=logging.DEBUG)
    root_logger.addHandler(log_handler)
    
    # Set root logger level to INFO to capture application messages (not AWS DEBUG noise)
    root_logger.setLevel(logging.INFO)
    
    # Redirect print() statements to also go through logging
    import sys
    
    class PrintToLogger:
        def __init__(self, logger, level=logging.INFO):
            self.logger = logger
            self.level = level
            self.terminal = sys.stdout
        
        def write(self, message):
            # Write to terminal first (for immediate display)
            self.terminal.write(message)
            self.terminal.flush()
            
            # Also log the message if it's not empty and not just whitespace
            # But only log to session logger, not to console (to avoid duplicates)
            if message.strip():
                # Remove trailing newlines for logging
                clean_message = message.rstrip('\n\r')
                if clean_message:
                    # Log directly to session logger without going through console handlers
                    if simple_session_logger.active_sessions:
                        simple_session_logger.log_message(clean_message, "INFO")
        
        def flush(self):
            self.terminal.flush()
    
    # Redirect stdout to our custom logger
    sys.stdout = PrintToLogger(root_logger, logging.INFO)
    
    print(f"✅ SimpleLogHandler added to root logger (now has {len(root_logger.handlers)} handlers)")
    logging.info("Simple session logging configured")
    
    # Test the handler
    logging.info("🧪 TEST: Simple logging setup complete")


# Convenience functions
def start_session(user_id: str, session_id: str, websocket_id: str = None):
    """Start session logging"""
    start_session_logging(user_id, session_id, websocket_id)

def stop_session(user_id: str, session_id: str, websocket_id: str = None):
    """Stop session logging"""
    stop_session_logging(user_id, session_id, websocket_id)
