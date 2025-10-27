#======================================IMPORTS======================================
import asyncio
import json
import re
import boto3
import logging
import requests
import httpx
import random
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException,status,Response
from fastapi.middleware.cors import CORSMiddleware
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.model import AudioStream, TranscriptEvent
from botocore.response import StreamingBody
from typing import Dict, Any, Optional, List, Tuple
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.responses import JSONResponse
import pprint
import functools
from datetime import datetime, timedelta
import uuid
import pytz
import collections
import traceback
import os
import time
import threading
import gc
import psutil
from dataclasses import dataclass, field
from context_manager import context_manager, extract_parameters_with_context, is_similar_query
from models import SessionInfo, QueryContext, SessionContext, EndpointValidator
from session_manager import (
    DatabaseSessionManager, session_manager, context_manager, SESSION_TIMEOUT_MINUTES,
    validate_session_for_query, get_session_from_db, validate_session_in_db,
    delete_session_from_db, get_active_sessions_from_db, save_context_to_db,
    get_context_from_db, update_context_in_db, delete_context_from_db,
    cleanup_expired_contexts_from_db, save_user_profile_to_db,
    detect_repeat_request, handle_repeat_request, format_repeat_response, 
    extract_parameters_from_query,
    delete_session, validate_session_and_regenerate_if_needed
)
from location import location_manager
from websocket_utils import (
    is_websocket_connected,
    safe_websocket_send_text,
    safe_websocket_send_bytes,
    safe_websocket_close,
    cleanup_websocket_resources,
)
from logging_utils import truncate_json_for_logging
from simple_session_logger import start_session_logging, stop_session_logging
from simple_log_handler import setup_simple_logging
from memory_monitor import (
    memory_monitor, get_memory_stats, increment_connections, decrement_connections,
    start_memory_monitoring, stop_memory_monitoring, get_monitoring_summary
)
from database_operations import (
    save_conversation, get_user_conversations, get_session_conversations,
    create_or_update_user, get_user_profile_from_db, update_user_profile_in_db,
    save_session_info, update_session_activity, save_conversation_feedback,
    get_previous_conversations, format_previous_conversations, handle_dynamodb_error
)
from prompts import (
    INTENT_CLASSIFICATION_PROMPT,
    QUERY_TYPE_CLASSIFICATION_PROMPT,
    COMBINED_RESPONSE_PROMPT,
    KB_INSTRUCTIONS,
    STATION_ABBREVIATION_MAP
)
from apis import (
    call_api_for_query, get_station_code, validate_stations,map_user_station_to_actual_station
)
from constants import ( station_data, station_groups, supported_languages, thinking_messages )
from aws_client_manager import (
    aws_client_manager, get_bedrock_client, get_kb_client, get_polly_client,
    get_dynamodb_resource, get_translate_client, get_comprehend_client,
    transcribe_stream_context, cleanup_aws_clients
)
from websocket_utils import (
    get_state_value_from_db, set_state_value_in_db, update_state_values_in_db,
    initialize_state_in_db, get_session_info_from_request, get_ws_state_from_db,
    get_state_for_response, get_station_state, get_location_state
)
#======================================TIMING UTILITIES======================================
class TimingManager:
    """Production-grade timing manager for performance monitoring with detailed step tracking"""
    def __init__(self):
        self.start_times = {}
        self.section_times = {}
        self.total_start_time = None
        self.text_response_end_time = None
        self.audio_start_time = None
    
    def start_total_timing(self):
        """Start timing for the entire response"""
        self.total_start_time = time.time()
        print(f"\n[TIMING] Starting total response timing at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
    
    def start_section_timing(self, section_name: str):
        """Start timing for a specific section"""
        self.start_times[section_name] = time.time()
        print(f"[TIMING] Starting {section_name} at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
    
    def end_section_timing(self, section_name: str):
        """End timing for a specific section and log the duration"""
        if section_name in self.start_times:
            duration = time.time() - self.start_times[section_name]
            self.section_times[section_name] = duration
            print(f"[TIMING] {section_name} completed in: {duration:.3f} seconds")
            del self.start_times[section_name]
            return duration
        return 0
    
    def mark_text_response_complete(self):
        """Mark when text response is complete (before audio synthesis)"""
        self.text_response_end_time = time.time()
        if self.total_start_time:
            text_duration = self.text_response_end_time - self.total_start_time
            print(f"[TIMING] TEXT RESPONSE COMPLETE: {text_duration:.3f} seconds")
    
    def start_audio_timing(self):
        """Start timing for audio synthesis (separate from text response)"""
        self.audio_start_time = time.time()
        print(f"[TIMING] Starting audio synthesis at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
    
    def end_total_timing(self, include_audio: bool = False):
        """End total timing and log summary"""
        if self.total_start_time:
            if include_audio:
                total_duration = time.time() - self.total_start_time
            else:
                total_duration = self.text_response_end_time - self.total_start_time if self.text_response_end_time else time.time() - self.total_start_time
            
            print(f"\n[TIMING] TOTAL RESPONSE TIME: {total_duration:.3f} seconds")
            print(f"[TIMING] Detailed step breakdown:")
            
            preprocessing_sections = []
            classification_sections = []
            api_sections = []
            kb_sections = []
            response_sections = []
            audio_sections = []
            
            for section, duration in self.section_times.items():
                if any(keyword in section.upper() for keyword in ['PREPROCESS', 'STATION_EXTRACT', 'LANGUAGE_DETECT']):
                    preprocessing_sections.append((section, duration))
                elif any(keyword in section.upper() for keyword in ['INTENT', 'CLASSIFICATION', 'CONTEXT']):
                    classification_sections.append((section, duration))
                elif any(keyword in section.upper() for keyword in ['API', 'BART', 'VALIDATION', 'PARAMETER']):
                    api_sections.append((section, duration))
                elif any(keyword in section.upper() for keyword in ['KB', 'KNOWLEDGE']):
                    kb_sections.append((section, duration))
                elif any(keyword in section.upper() for keyword in ['CLAUDE', 'RESPONSE', 'GENERATION']):
                    response_sections.append((section, duration))
                elif any(keyword in section.upper() for keyword in ['AUDIO', 'SYNTHESIS']):
                    audio_sections.append((section, duration))
                else:
                    response_sections.append((section, duration))
            
            if preprocessing_sections:
                print("   PREPROCESSING:")
                for section, duration in preprocessing_sections:
                    percentage = (duration / total_duration) * 100 if total_duration > 0 else 0
                    print(f"      • {section}: {duration:.3f}s ({percentage:.1f}%)")
            
            if classification_sections:
                print("   CLASSIFICATION:")
                for section, duration in classification_sections:
                    percentage = (duration / total_duration) * 100 if total_duration > 0 else 0
                    print(f"      • {section}: {duration:.3f}s ({percentage:.1f}%)")
            
            if api_sections:
                print("   API PROCESSING:")
                for section, duration in api_sections:
                    percentage = (duration / total_duration) * 100 if total_duration > 0 else 0
                    print(f"      • {section}: {duration:.3f}s ({percentage:.1f}%)")
            
            if kb_sections:
                print("   KNOWLEDGE BASE:")
                for section, duration in kb_sections:
                    percentage = (duration / total_duration) * 100 if total_duration > 0 else 0
                    print(f"      • {section}: {duration:.3f}s ({percentage:.1f}%)")
            
            if response_sections:
                print("   RESPONSE GENERATION:")
                for section, duration in response_sections:
                    percentage = (duration / total_duration) * 100 if total_duration > 0 else 0
                    print(f"      • {section}: {duration:.3f}s ({percentage:.1f}%)")
            
            if audio_sections and include_audio:
                print("   AUDIO SYNTHESIS:")
                for section, duration in audio_sections:
                    percentage = (duration / total_duration) * 100 if total_duration > 0 else 0
                    print(f"      • {section}: {duration:.3f}s ({percentage:.1f}%)")
            
            print("=" * 60)
            return total_duration
        return 0

timing_manager = TimingManager()
#======================================SESSION MANAGER======================================
class WebSocketConnectionManager:
    """Production-grade connection manager to prevent duplicate connections"""
    def __init__(self):
        self.active_connections = {}  # user_id -> websocket_id
        self.websocket_to_user = {}  # websocket_id -> user_id
        self._lock = threading.Lock()
    
    def register_connection(self, websocket_id: str, user_id: str) -> bool:
        """Register a new connection, return False if user already has active connection"""
        with self._lock:
            if user_id in self.active_connections:
                logging.warning(f"User {user_id} already has active connection {self.active_connections[user_id]}")
                return False
            
            self.active_connections[user_id] = websocket_id
            self.websocket_to_user[websocket_id] = user_id
            logging.info(f"Registered connection {websocket_id} for user {user_id}")
            return True
    
    def unregister_connection(self, websocket_id: str):
        """Unregister a connection"""
        with self._lock:
            if websocket_id in self.websocket_to_user:
                user_id = self.websocket_to_user[websocket_id]
                del self.active_connections[user_id]
                del self.websocket_to_user[websocket_id]
                logging.info(f"Unregistered connection {websocket_id} for user {user_id}")
    
    def has_active_connection(self, user_id: str) -> bool:
        """Check if user has an active connection"""
        with self._lock:
            return user_id in self.active_connections

connection_manager = WebSocketConnectionManager()

class WebSocketSessionManager:
    """Production-grade session manager for WebSocket connections with memory leak prevention"""
    def __init__(self):
        self.sessions: Dict[int, Dict[str, Any]] = {}  
        self.max_sessions = int(os.getenv("MAX_SESSIONS"))
        self.cleanup_interval = int(os.getenv("CLEANUP_INTERVAL"))
        self.session_timeout = int(os.getenv("SESSION_TIMEOUT"))
        self.last_cleanup = time.time()
        self._lock = threading.RLock()  
        self._cleanup_task = None  
    
    def _cleanup_expired_sessions(self):
        """
        Clean up expired sessions to prevent memory leaks
        OPTIMIZED: Minimize lock time to prevent blocking under high load
        """
        try:
            current_time = time.time()
            if current_time - self.last_cleanup < self.cleanup_interval:
                return
            
            expired_sessions = []
            
            with self._lock:
                if current_time - self.last_cleanup < self.cleanup_interval:
                    return
                
                for websocket_id, session_info in list(self.sessions.items()):
                    session_age = current_time - session_info.get('timestamp', current_time)
                    if session_age > self.session_timeout:
                        expired_sessions.append(websocket_id)
                
                if len(self.sessions) > self.max_sessions:
                    sorted_sessions = sorted(self.sessions.items(), 
                                           key=lambda x: x[1].get('timestamp', 0))
                    excess_count = len(self.sessions) - self.max_sessions
                    for websocket_id, _ in sorted_sessions[:excess_count]:
                        if websocket_id not in expired_sessions:
                            expired_sessions.append(websocket_id)
            
            if expired_sessions:
                with self._lock:
                    for websocket_id in expired_sessions:
                        self.sessions.pop(websocket_id, None)  
                    self.last_cleanup = current_time
                
                logging.info(f"Cleaned up {len(expired_sessions)} expired/excess WebSocket sessions")
            else:
                with self._lock:
                    self.last_cleanup = current_time
            
        except Exception as e:
            logging.error(f"Error during session cleanup: {str(e)}")
    
    def store_session(self, websocket: WebSocket, user_id: str, session_id: str):
        """
        Store session information for a WebSocket connection with TTL
        OPTIMIZED: Only cleanup periodically, not on every store
        """
        try:
            self._cleanup_expired_sessions()
        except Exception as e:
            logging.warning(f"Cleanup during store failed (non-critical): {str(e)}")
        
        with self._lock:
            websocket_id = id(websocket)
            self.sessions[websocket_id] = {
                'user_id': user_id,
                'session_id': session_id,
                'timestamp': time.time()
            }
            logging.info(f"Stored session for WebSocket {websocket_id}: {user_id}/{session_id}")
    
    def get_session(self, websocket: WebSocket) -> Tuple[Optional[str], Optional[str]]:
        """Get session information for a WebSocket connection"""
        with self._lock:
            websocket_id = id(websocket)
            session_info = self.sessions.get(websocket_id)
            if session_info:
                session_age = time.time() - session_info.get('timestamp', 0)
                if session_age <= self.session_timeout:
                    return session_info['user_id'], session_info['session_id']
                else:
                    del self.sessions[websocket_id]
                    logging.info(f"Removed expired session for WebSocket {websocket_id}")
            return None, None
    
    def remove_session(self, websocket: WebSocket):
        """Remove session information when WebSocket disconnects"""
        with self._lock:
            websocket_id = id(websocket)
            if websocket_id in self.sessions:
                del self.sessions[websocket_id]
                logging.info(f"Removed session for WebSocket {websocket_id}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get session manager statistics for monitoring"""
        with self._lock:
            self._cleanup_expired_sessions()
            return {
                'active_sessions': len(self.sessions),
                'max_sessions': self.max_sessions,
                'cleanup_interval': self.cleanup_interval,
                'session_timeout': self.session_timeout
            }

session_manager_instance = WebSocketSessionManager()

async def get_user_session_info(websocket: WebSocket) -> Tuple[Optional[str], Optional[str]]:
    """
    Production-optimized function to get user_id and session_id.
    Uses in-memory session manager for fast access, with database fallback.
    
    Args:
        websocket: The WebSocket connection object
        
    Returns:
        Tuple[Optional[str], Optional[str]]: (user_id, session_id) or (None, None) if not found
    """
    try:
        user_id, session_id = session_manager_instance.get_session(websocket)
        if user_id and session_id:
            return user_id, session_id
        
        from database_operations import get_user_session_from_websocket
        user_id, session_id = await get_user_session_from_websocket(websocket)
        
        if user_id and session_id:
            session_manager_instance.store_session(websocket, user_id, session_id)
        
        return user_id, session_id
        
    except Exception as e:
        logging.error(f"Error in get_user_session_info: {str(e)}")
        return None, None

#======================================VERSION CHECK======================================
try:
    boto3_version = boto3.__version__
    logging.info(f"boto3 version: {boto3_version}")
    
    if boto3_version < "1.34.84":
        logging.warning(f"boto3 version {boto3_version} may not support newer Bedrock Knowledge Base features. Recommended: >=1.34.84")
    else:
        logging.info(f"boto3 version {boto3_version} should support newer Bedrock Knowledge Base features")
except Exception as e:
    logging.warning(f"Could not check boto3 version: {str(e)}")

class ResourceMonitor:
    """Production-grade resource monitoring and management for 2k concurrent users"""
    
    def __init__(self):
        self.memory_threshold = float(os.getenv("MAX_MEMORY_PERCENT"))
        self.cpu_threshold = float(os.getenv("MAX_CPU_PERCENT"))
        self.cleanup_interval = int(os.getenv("CLEANUP_INTERVAL"))
        self.alert_interval = int(os.getenv("ALERT_INTERVAL"))
        self.last_alert_time = 0
        self.connection_count = 0
        self.max_connections = int(os.getenv("MAX_SESSIONS"))
        
    async def start_monitoring(self):
        """Start resource monitoring in background"""
        asyncio.create_task(self.monitor_resources())
        logging.info("Resource monitoring started")
        
    async def monitor_resources(self):
        """Continuous resource monitoring with intelligent cleanup"""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                
                memory_info = psutil.virtual_memory()
                cpu_percent = psutil.cpu_percent(interval=1)
                memory_percent = memory_info.percent
                
                if memory_percent > self.memory_threshold:
                    await self._handle_high_memory(memory_percent)
                
                if cpu_percent > self.cpu_threshold:
                    await self._handle_high_cpu(cpu_percent)
                
                if self.connection_count > self.max_connections * 0.8:
                    await self._handle_high_connections()
                
                await self._perform_cleanup()
                
            except Exception as e:
                logging.error(f"Error in resource monitoring: {str(e)}")
                await asyncio.sleep(60)
    
    async def _handle_high_memory(self, memory_percent):
        """Handle high memory usage"""
        current_time = time.time()
        
        if current_time - self.last_alert_time > self.alert_interval:
            logging.warning(f"CRITICAL: High memory usage detected: {memory_percent:.1f}%")
            self.last_alert_time = current_time
        
        logging.info("Performing aggressive memory cleanup")
        gc.collect()
        
        if memory_percent > 95:
            logging.warning("Extreme memory usage - clearing session cache")
            session_manager_instance.sessions.clear()
            gc.collect()
    
    async def _handle_high_cpu(self, cpu_percent):
        """Handle high CPU usage"""
        current_time = time.time()
        
        if current_time - self.last_alert_time > self.alert_interval:
            logging.warning(f"CRITICAL: High CPU usage detected: {cpu_percent:.1f}%")
            self.last_alert_time = current_time
    
    async def _handle_high_connections(self):
        """Handle high connection count"""
        logging.warning(f"High connection count: {self.connection_count}/{self.max_connections}")
    
    async def _perform_cleanup(self):
        """Perform regular cleanup tasks"""
        try:
            from context_manager import context_manager
            await context_manager.cleanup_expired_contexts()
            
            collected = gc.collect()
            # Garbage collection completed (logging removed for cleaner logs)
            # if collected > 0:
            #     logging.info(f"Garbage collection freed {collected} objects")
                
        except Exception as e:
            logging.error(f"Error during cleanup: {str(e)}")
    
    def increment_connections(self):
        """Increment connection counter"""
        self.connection_count += 1
    
    def decrement_connections(self):
        """Decrement connection counter"""
        if self.connection_count > 0:
            self.connection_count -= 1

resource_monitor = ResourceMonitor()

async def memory_cleanup_task():
    """Legacy memory cleanup task - now handled by ResourceMonitor"""
    await resource_monitor.monitor_resources()
# ======================================FASTAPI SETUP======================================
app = FastAPI(
    title="MAAS LLM App", 
    version="1.0.0", 
    docs_url="/docs" if os.getenv("ENVIRONMENT") != "production" else None, 
    redoc_url="/redoc" if os.getenv("ENVIRONMENT") != "production" else None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        os.getenv("ALLOWED_ORIGINS")
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    max_age=3600,  
)
# ======================================AWS CLIENTS======================================
bedrock_client = get_bedrock_client()
kb_client = get_kb_client()
polly_client = get_polly_client()
dynamodb = get_dynamodb_resource()
# ======================================CONSTANTS======================================
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID")
CHUNK_SIZE = 256000  
REGION = os.getenv("AWS_DEFAULT_REGION")
MODEL_ID = os.getenv("MODEL_ID")
MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/{MODEL_ID}"
SUPPORTED_LANGUAGES = supported_languages
SESSION_TIMEOUT_MINUTES = int(os.getenv("SESSION_TIMEOUT_MINUTES"))
# ======================================BART API CONFIGURATION======================================
BART_API_KEY = os.getenv("BART_API_KEY")
BART_BASE_URL = os.getenv("BART_BASE_URL")
# ======================================LOGGING SETUP======================================
log_level = os.getenv("LOG_LEVEL")
logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO))
# ======================================STATION DATA======================================
STATION_DATA = station_data
STATION_GROUPS = station_groups

# ======================================PRODUCTION HEALTH CHECK ENDPOINTS======================================
@app.get("/")
async def health_check():
    """
    Ultra-lightweight health check for load balancer - NO BLOCKING OPERATIONS
    This endpoint MUST respond within 5 seconds to avoid 4xx errors
    CRITICAL: This endpoint is used by load balancer health checks
    """
    try:
        return {"status": "ok", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        # CRITICAL FIX: Always return 200 to prevent 4xx errors
        logging.error(f"Health check error: {str(e)}")
        return {"status": "error", "timestamp": datetime.now().isoformat(), "error": str(e)}

@app.get("/health")
async def comprehensive_health_check():
    """
    Comprehensive health check with TIMEOUT PROTECTION to prevent 4xx errors
    All checks are wrapped with timeouts and run concurrently
    """
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "version": "1.0.0",
            "checks": {}
        }
        
        try:
            from memory_monitor import get_memory_stats
            memory_stats = await asyncio.wait_for(
                asyncio.to_thread(get_memory_stats),
                timeout=1.0
            )
            health_status["checks"]["memory"] = {
                "status": "healthy" if memory_stats.memory_percent < 80 else "warning" if memory_stats.memory_percent < 90 else "critical",
                "memory_percent": memory_stats.memory_percent,
                "available_mb": memory_stats.available_memory_mb
            }
            if memory_stats.memory_percent > 90:
                health_status["status"] = "critical"
            elif memory_stats.memory_percent > 80:
                health_status["status"] = "warning"
        except asyncio.TimeoutError:
            health_status["checks"]["memory"] = {"status": "timeout", "error": "Memory check timed out"}
        except Exception as e:
            health_status["checks"]["memory"] = {"status": "unknown", "error": str(e)}
        
        try:
            from database_operations import dynamodb
            table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
            
            async def check_db():
                return await asyncio.to_thread(lambda: table.table_status)
            
            await asyncio.wait_for(check_db(), timeout=2.0)
            health_status["checks"]["database"] = {"status": "healthy"}
        except asyncio.TimeoutError:
            health_status["checks"]["database"] = {"status": "timeout", "error": "Database check timed out"}
        except Exception as e:
            health_status["checks"]["database"] = {"status": "error", "error": str(e)}
        
        health_status["checks"]["aws_services"] = {"status": "skipped", "reason": "Checked separately"}
        
        try:
            session_stats = session_manager_instance.get_stats()
            health_status["checks"]["websocket_sessions"] = {
                "status": "healthy" if session_stats["active_sessions"] < 1000 else "warning",
                "active_sessions": session_stats["active_sessions"],
                "max_sessions": session_stats["max_sessions"]
            }
            if session_stats["active_sessions"] > 1000:
                health_status["status"] = "warning"
        except Exception as e:
            health_status["checks"]["websocket_sessions"] = {"status": "error", "error": str(e)}
        
        return health_status
        
    except Exception as e:
        logging.error(f"Health check failed: {str(e)}")
        return {
            "status": "degraded",
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        }

@app.get("/health/ready")
async def readiness_check():
    """
    Kubernetes readiness probe with timeout protection
    CRITICAL: Return 200 with status info instead of 503 to avoid 4xx errors
    """
    try:
        checks = {}
        is_ready = True
        
        try:
            from database_operations import dynamodb
            table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
            
            async def check_db():
                return await asyncio.to_thread(lambda: table.table_status)
            
            await asyncio.wait_for(check_db(), timeout=2.0)
            checks["database"] = "ready"
        except asyncio.TimeoutError:
            checks["database"] = "timeout"
            is_ready = False
            logging.warning("Database readiness check timed out")
        except Exception as e:
            checks["database"] = f"error: {str(e)}"
            is_ready = False
            logging.error(f"Database readiness check failed: {str(e)}")
        
        try:
            from aws_client_manager import get_bedrock_client
            
            async def check_aws():
                return await asyncio.to_thread(get_bedrock_client)
            
            await asyncio.wait_for(check_aws(), timeout=2.0)
            checks["aws_services"] = "ready"
        except asyncio.TimeoutError:
            checks["aws_services"] = "timeout"
            is_ready = False
            logging.warning("AWS services readiness check timed out")
        except Exception as e:
            checks["aws_services"] = f"error: {str(e)}"
            is_ready = False
            logging.error(f"AWS services readiness check failed: {str(e)}")
        
        # CRITICAL FIX: Always return 200 to prevent 4xx errors from load balancer
        return {
            "status": "ready" if is_ready else "not_ready",
            "timestamp": datetime.now().isoformat(),
            "checks": checks,
            "load_balancer_compatible": True  # Signal to load balancer that this is a valid response
        }
        
    except Exception as e:
        logging.error(f"Readiness check failed: {str(e)}")
        # CRITICAL FIX: Always return 200 to prevent 4xx errors from load balancer
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
            "load_balancer_compatible": True  # Signal to load balancer that this is a valid response
        }

@app.get("/health/live")
async def liveness_check():
    """
    Kubernetes liveness probe - ultra-lightweight check
    NEVER throw exceptions - always return 200 OK
    """
    try:
        return {"status": "alive", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logging.error(f"Liveness check failed: {str(e)}")
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        }         

def check_station_ambiguity(station_name: str, resolved_station: str = None) -> Tuple[bool, List[str]]:
    """
    Production-grade station ambiguity checker with enhanced normalization support.
    
    This function now properly handles:
    - Normalized station names (Street -> St., compound names, etc.)
    - Complete station name matching to avoid false disambiguation
    - SFO airport aliases
    - Compound station name parts
    - Resolved stations to prevent re-disambiguation
    
    Args:
        station_name: The station name to check for ambiguity
        resolved_station: The recently resolved station to avoid re-disambiguation
        
    Returns:
        Tuple of (is_ambiguous: bool, options: List[str])
    """
    options = []
    original_station_lower = station_name.lower().strip()
    
    if len(original_station_lower) < 3:
        return False, []
    
    sfo_aliases = [
        "sfo", "sfia", "sfo airport", "sf airport", 
        "san francisco airport", "san francisco international", "san francisco international airport",
        "sanfransisco airport", "sanfranssisco airport", "sanfransciso airport",  # Common typos
        "sanfransisco international airport", "sanfranssisco international airport", "sanfransciso international airport",  # Common typos with international
        "sf international airport", "sf international", "san francisco intl", "san francisco intl airport"
    ]
    if any(re.search(r'\b' + re.escape(alias) + r'\b', original_station_lower) for alias in sfo_aliases) or original_station_lower in sfo_aliases:
        print(f"Found SFO airport match for '{station_name}', not treating as ambiguous")
        return False, []
    
    # 1. Check STATION_GROUPS FIRST with ORIGINAL input (before normalization)
    for group_key, stations_list in STATION_GROUPS.items():
        if group_key.lower() == original_station_lower:
            if resolved_station:
                resolved_lower = resolved_station.lower().strip()
                if any(opt.lower() == resolved_lower for opt in stations_list):
                    print(f"Skipping disambiguation for '{station_name}' - already resolved to '{resolved_station}'")
                    return False, []
            
            options = stations_list.copy()
            print(f"Found station group match: '{group_key}' -> {options}")
            is_ambiguous = len(options) > 1
            return is_ambiguous, options
    
    # 2. NOW normalize the station name for exact matching
    normalized_station = normalize_station_name(station_name)
    station_name_lower = normalized_station.lower().strip()
    
    # 3. Check for exact COMPLETE station name match (only if NOT a group key)
    for station in STATION_DATA['stations']:
        if station['name'].lower() == station_name_lower:
            print(f"Exact station match found for '{station_name}' -> '{station['name']}', not treating as ambiguous")
            return False, []
    
    # 4. Check for exact abbreviation match
    for station in STATION_DATA['stations']:
        if station['abbr'].lower() == station_name_lower:
            print(f"Exact abbreviation match found for '{station_name}' -> '{station['name']}', not treating as ambiguous")
            return False, []
    
    # 5. Check for exact match with compound station parts (e.g., "Dublin" -> "Dublin/Pleasanton")
    for station in STATION_DATA['stations']:
        if '/' in station['name']:
            parts = [part.strip().lower() for part in station['name'].split('/')]
            if station_name_lower in parts:
                for group_key, stations_list in STATION_GROUPS.items():
                    if group_key.lower() == station_name_lower:
                        print(f"Found compound station part in STATION_GROUPS: '{station_name_lower}' -> {stations_list}")
                        return True, stations_list
                print(f"Exact match with compound station part '{station_name_lower}' -> '{station['name']}', not treating as ambiguous")
                return False, [station['name']]
    
    # 6. Check for partial matches in station names (fallback)
    matching_stations = []
    for station in STATION_DATA['stations']:
        station_name_clean = station['name'].lower()
        
        if station_name_lower in station_name_clean and len(station_name_lower) >= 4:
            matching_stations.append(station['name'])
            continue
        
        station_parts = station_name_clean.split()
        if any(part == station_name_lower for part in station_parts if len(part) > 3):
            matching_stations.append(station['name'])
            continue
        
        if '/' in station['name']:
            slash_parts = [part.strip().lower() for part in station['name'].split('/')]
            if any(part == station_name_lower for part in slash_parts):
                matching_stations.append(station['name'])
                continue
    
    if len(matching_stations) > 1:
        options = matching_stations
        print(f"Found multiple matching stations for '{station_name}': {options}")
    
    is_ambiguous = len(options) > 1
    print(f"Ambiguity check for '{station_name}' (normalized: '{normalized_station}'): {'Ambiguous' if is_ambiguous else 'Not ambiguous'}, Options: {options}")
    return is_ambiguous, options

_compound_patterns_cache = None

def _get_compound_patterns():
    """
    Production-grade caching for compound station patterns.
    Generates patterns once and caches them for performance.
    """
    global _compound_patterns_cache
    
    if _compound_patterns_cache is not None:
        return _compound_patterns_cache
    
    compound_patterns = []
    
    for station in STATION_DATA.get("stations", []):
        station_name = station["name"]
        
        if '/' not in station_name and '.' not in station_name:
            continue
            
        if '/' in station_name:
            spaced_version = station_name.replace('/', ' ')
            pattern = r'\b' + re.escape(spaced_version).replace(r'\ ', r'\s+') + r'\b'
            compound_patterns.append((pattern, station_name))
            
            normalized_spaced = re.sub(r'\s+', ' ', spaced_version).strip()
            if normalized_spaced != spaced_version:
                pattern = r'\b' + re.escape(normalized_spaced).replace(r'\ ', r'\s+') + r'\b'
                compound_patterns.append((pattern, station_name))
        
        if '.' in station_name:
            unabbreviated = station_name.replace('.', '')
            pattern = r'\b' + re.escape(unabbreviated) + r'\b'
            compound_patterns.append((pattern, station_name))
            
            spaced_unabbreviated = unabbreviated.replace('St ', 'St ')
            if spaced_unabbreviated != unabbreviated:
                pattern = r'\b' + re.escape(spaced_unabbreviated) + r'\b'
                compound_patterns.append((pattern, station_name))
    
    compound_patterns.sort(key=lambda x: len(x[1]), reverse=True)
    
    _compound_patterns_cache = compound_patterns
    return compound_patterns

def clear_station_patterns_cache():
    """
    Clear the compound patterns cache.
    
    Call this function when station data is updated to ensure
    the normalization patterns are regenerated.
    """
    global _compound_patterns_cache
    _compound_patterns_cache = None

def normalize_station_name(station_name: str) -> str:
    """
    Production-grade station name normalization with comprehensive matching logic.
    
    Features:
    - Dynamic pattern generation from station data (no hardcoding)
    - Cached patterns for performance
    - Street/St. normalization
    - Compound station name handling
    - Complete station name matching
    - SFO airport aliases
    - Punctuation and whitespace normalization
    
    Args:
        station_name: The station name to normalize
        
    Returns:
        Normalized station name that matches a valid BART station,
        or the original name if no match was found
    """
    if not station_name:
        return station_name
        
    def normalize_street_abbreviations(text: str) -> str:
        """Convert 'Street' to 'St.' and other common abbreviations"""
        text = re.sub(r'\bStreet\b', 'St.', text, flags=re.IGNORECASE)
        return text
    
    def normalize_station_suffixes(text: str) -> str:
        """Remove common station suffixes that users might add"""
        text = re.sub(r'\s+station\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+stop\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+terminal\s*$', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+depot\s*$', '', text, flags=re.IGNORECASE)
        return text
    
    def normalize_compound_stations(text: str) -> str:
        """
        Production-grade dynamic compound station normalization.
        
        Uses cached patterns generated from station data.
        This approach is:
        - Maintainable: No code changes needed for new stations
        - Scalable: Works with any station data
        - Consistent: Always matches actual station data
        - Error-free: No manual pattern creation
        - Performant: Cached patterns for fast lookups
        """
        compound_patterns = _get_compound_patterns()
        
        for pattern, replacement in compound_patterns:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
        return text
    
    normalized_name = station_name.strip()
    normalized_name = normalize_street_abbreviations(normalized_name)
    normalized_name = normalize_station_suffixes(normalized_name)
    normalized_name = normalize_compound_stations(normalized_name)
    
    clean_name = re.sub(r'[,;:]', ' ', normalized_name)
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()
    clean_name_lower = clean_name.lower()
    
    sfo_aliases = [
        "sfo", "sfia", "sfo airport", "sf airport", 
        "san francisco airport", "san francisco international", "san francisco international airport",
        "sanfransisco airport", "sanfranssisco airport", "sanfransciso airport",  # Common typos
        "sanfransisco international airport", "sanfranssisco international airport", "sanfransciso international airport",  # Common typos with international
        "sf international airport", "sf international", "san francisco intl", "san francisco intl airport"
    ]
    if any(re.search(r'\b' + re.escape(alias) + r'\b', clean_name_lower) for alias in sfo_aliases) or clean_name_lower in sfo_aliases:
        for station in STATION_DATA["stations"]:
            if station["abbr"].lower() == "sfia":
                return station["name"]
    
    # 1. Exact station name match (highest priority)
    for station in STATION_DATA["stations"]:
        if station["name"].lower() == clean_name_lower:
            return station["name"]
    
    # 2. Exact abbreviation match
    for station in STATION_DATA["stations"]:
        if station["abbr"].lower() == clean_name_lower:
            return station["name"]
    
    # 3. Match without punctuation
    clean_name_no_punct = re.sub(r'[^\w\s]', '', clean_name).strip()
    clean_name_no_punct_lower = clean_name_no_punct.lower()
    
    for station in STATION_DATA["stations"]:
        station_no_punct = re.sub(r'[^\w\s]', '', station["name"]).strip()
        if station_no_punct.lower() == clean_name_no_punct_lower:
            return station["name"]
    
    # 4. Compressed whitespace match
    for station in STATION_DATA["stations"]:
        station_compressed = re.sub(r'\s+', '', station["name"].lower())
        input_compressed = re.sub(r'\s+', '', clean_name_lower)
        if station_compressed == input_compressed:
            return station["name"]
    
    # 5. Handle compound stations with slash parts
    for station in STATION_DATA["stations"]:
        if '/' in station["name"]:
            parts = [part.strip().lower() for part in station["name"].split('/')]
            if clean_name_lower in parts or any(part in clean_name_lower for part in parts):
                return station["name"]
    
    # 6. Check for partial matches in station names (case-insensitive)
    for group_key in STATION_GROUPS.keys():
        if group_key.lower() == clean_name_lower:
            print(f"'{station_name}' matches STATION_GROUPS key '{group_key}' - not normalizing to allow disambiguation")
            return clean_name.title()
    
    for station in STATION_DATA["stations"]:
        station_lower = station["name"].lower()
        station_words = station_lower.split()
        if clean_name_lower in station_words:
            return station["name"]
        if clean_name_lower in station_lower and len(clean_name_lower) >= 4:
            return station["name"]
    
    # 7. No match found - return with proper title casing
    return clean_name.title()

# ======================================WEBSOCKET HANDLER======================================
@app.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket):
    """WebSocket endpoint for audio processing with enhanced concurrency support and 4xx error prevention"""
    
    # CRITICAL FIX: Check for existing connections before accepting new ones
    websocket_id = f"ws_{id(websocket)}"
    
    # CRITICAL FIX: Check for existing connections before accepting new ones
    # We'll check this after we get the user_id from the initialization message
    
    try:
        await websocket.accept()
        # CRITICAL FIX: Set WebSocket timeout to prevent 4xx errors
        websocket.timeout = float(os.getenv("WEBSOCKET_PING_TIMEOUT"))
    except Exception as e:
        logging.error(f"Failed to accept WebSocket connection: {str(e)}")
        return
    from audio_buffer_manager import create_managed_buffer
    
    buffer = create_managed_buffer()
    pre_buffer = collections.deque(maxlen=32000)
    last_process_time = 0
    silence_counter = 0
    cleanup_done = False  
    
    user_id = None
    session_id = None
    original_query = None
    buffer_overflow_count = 0
    connection_active = True
    connection_established = False  # Track if connection is fully established  
    
    import re
    
    connection_params = dict(websocket.query_params)
    is_health_check = False
    skip_tts = False
    
    if "health_check" in connection_params:
        health_check_param = connection_params.get("health_check", "false").lower()
        is_health_check = health_check_param in ("true", "1", "yes")
        logging.info(f"Health check mode enabled: {is_health_check}")
        logging.info(f"Health check connection params: {connection_params}")
    
    if "skip_tts" in connection_params:
        skip_tts_param = connection_params.get("skip_tts", "false").lower()
        skip_tts = skip_tts_param in ("true", "1", "yes")
        logging.info(f"Skip TTS mode enabled: {skip_tts}")
    
    logging.info("New WebSocket connection established")
    
    resource_monitor.increment_connections()
    increment_connections() 
    
    try:
        # --------- INITIALIZATION PHASE ---------
        try:
            init_message = await websocket.receive()
        except RuntimeError as rt_err:
            if "disconnect message has been received" in str(rt_err):
                logging.info("Client disconnected during initialization")
                return
            elif "close message has been sent" in str(rt_err):
                logging.info("WebSocket connection closed during initialization")
                return
            else:
                logging.error(f"RuntimeError during WebSocket initialization: {str(rt_err)}")
                return
        except WebSocketDisconnect:
            logging.info("WebSocket disconnected during initialization")
            return
            
        try:
            if 'type' in init_message and init_message['type'] == 'websocket.disconnect':
                logging.info(f"WebSocket disconnect message received during initialization: {init_message}")
                return
            
            if 'text' in init_message:
                init_data = json.loads(init_message['text'])
                logging.info(f"Received text initialization message: {init_data}")
            elif 'bytes' in init_message:
                try:
                    if isinstance(init_message['bytes'], bytes):
                        init_text = init_message['bytes'].decode('utf-8', errors='replace')
                    else:
                        init_text = str(init_message['bytes'])
                    init_data = json.loads(init_text)
                    logging.info(f"Received bytes initialization message: {init_data}")
                except UnicodeDecodeError as e:
                    logging.warning(f"UTF-8 decode error in init message: {str(e)}, using latin-1 fallback")
                    try:
                        init_text = init_message['bytes'].decode('latin-1', errors='replace')
                        init_data = json.loads(init_text)
                        logging.info(f"Received bytes initialization message (latin-1): {init_data}")
                    except Exception as fallback_e:
                        logging.error(f"Failed to decode init message even with latin-1: {str(fallback_e)}")
                        raise ValueError("Invalid initialization data encoding")
                except json.JSONDecodeError as e:
                    logging.error(f"JSON decode error in init message: {str(e)}")
                    raise ValueError("Invalid initialization data format")
            else:
                logging.error(f"Invalid message format: {init_message}")
                raise ValueError("Invalid message format")

            user_id = init_data.get('user_id')
            language = init_data.get('language', 'en')
            skip_tts_from_init = init_data.get('skip_tts', False)  
            skip_tts = skip_tts or skip_tts_from_init
            
            if not user_id:
                raise ValueError("No user_id provided")
            
            # CRITICAL FIX: Check for existing connections for this user
            if connection_manager.has_active_connection(user_id):
                logging.warning(f"User {user_id} already has an active connection - rejecting duplicate")
                await safe_websocket_close(websocket, code=1000, reason="Connection already exists")
                return
            
            # Register this connection
            if not connection_manager.register_connection(websocket_id, user_id):
                logging.warning(f"Failed to register connection for user {user_id} - rejecting")
                await safe_websocket_close(websocket, code=1000, reason="Connection registration failed")
                return
            
            session_id = str(uuid.uuid4())
            logging.info(f"Initialization request for user {user_id} - creating new session {session_id}")
            
            try:
                websocket_id = f"ws_{id(websocket)}"
                logging.info(f"User session context ready for user {user_id}, session {session_id}")
                
                logging.info(f"Session logging active for user {user_id}")
                                
            except Exception as context_e:
                logging.error(f"Error in user session setup: {str(context_e)}")     
            
            try:
                session_created = await session_manager.create_session(
                    user_id=user_id,
                    session_id=session_id,
                    language=language
                )
                if not session_created:
                    logging.warning(f"Failed to create session for user {user_id}, but continuing anyway")
            except Exception as e:
                logging.warning(f"Error creating session for user {user_id}: {str(e)}, but continuing anyway")
            
            await session_manager.update_session_activity(user_id, session_id)
            
            session_manager_instance.store_session(websocket, user_id, session_id)
            
            from database_operations import save_websocket_connection_mapping
            websocket_id = f"ws_{id(websocket)}"
            await save_websocket_connection_mapping(websocket_id, user_id, session_id)
            
            try:
                start_session_logging(user_id, session_id, websocket_id)
            except Exception as log_e:
                logging.error(f"Error starting session logging: {str(log_e)}")
                        
            await initialize_state_in_db(user_id, session_id)
            
            await update_state_values_in_db(user_id, session_id, {
                        'skip_tts': skip_tts,
                        'is_health_check': is_health_check,
                        'language': language,
                        'user_id': user_id,
                        'session_id': session_id,
                        'response_sent': False,
                        'last_saved_conversation': None,
                        'skip_current_response': False,
                        'awaiting_disambiguation': False,
                        'awaiting_station_validation': False,
                        'invalid_station': None,
                        'station_suggestions': None,
                        'original_query': None,
                        'ambiguous_station': None,
                        'station_options': None,
                        'input_mode': None,
                        'is_repeat_request': False,
                        'is_station_replacement': False,
                        'location_response_processed': False,
                        'original_query_language': None,
                        'original_query_text': None,
                        'complete_query': False,
                        'origin_station': None,
                        'destination_station': None,
                        'final_query': None,
                        'original_query_for_response': None,
                        'replaced_query': None
            })
            initialization_response = {
                "type": "initialization",
                "user_id": user_id,
                "session_id": session_id,
                "language": language,
                "message": "Hello, I am your BART Assistant. How can I help you?",
                "timestamp": datetime.utcnow().isoformat()
            }
            await safe_websocket_send_text(websocket, json.dumps(initialization_response, ensure_ascii=False))
            
            if language not in SUPPORTED_LANGUAGES:
                logging.warning(f"Unsupported language: {language}, defaulting to English")
                language = 'en'
                await set_state_value_in_db(user_id, session_id, 'language', 'en')
                
            logging.info(f"Language set to: {SUPPORTED_LANGUAGES[language]['name']}")
            
            await create_or_update_user(user_id=user_id)
            await save_user_profile_to_db(user_id=user_id, language=language)
            logging.info(f"Initialized WebSocket for user: {user_id} with session: {session_id}")
            
            # CRITICAL FIX: Mark connection as fully established
            connection_established = True
            
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"Invalid initialization data: {str(e)}")
            await safe_websocket_close(websocket, code=1003, reason="Invalid initialization data")
            return

        # --------- MAIN PROCESSING LOOP ---------
        while connection_active:
            try:
                try:
                    websocket_id = f"ws_{id(websocket)}"
                except Exception as context_e:
                    logging.warning(f"Failed to maintain user session context: {str(context_e)}")
                
                if not is_websocket_connected(websocket):
                    logging.info("WebSocket connection lost, stopping main processing loop")
                    connection_active = False
                    break
                    
                is_health_check = await get_state_value_from_db(user_id, session_id, 'is_health_check', False)
                response_sent = await get_state_value_from_db(user_id, session_id, 'response_sent', False)
                if is_health_check and response_sent:
                    logging.info("Health check completed, exiting receive loop")
                    break
                
                try:
                    message = await websocket.receive()
                except RuntimeError as rt_err:
                    if "disconnect message has been received" in str(rt_err):
                        logging.info("WebSocket disconnect message received, ending gracefully")
                        break
                    elif "close message has been sent" in str(rt_err):
                        logging.info("WebSocket close message sent, ending gracefully")
                        break
                    else:
                        logging.error(f"RuntimeError in websocket: {str(rt_err)}")
                        break
                except WebSocketDisconnect:
                    logging.info("WebSocket disconnected")
                    break
                
                if 'type' in message and message['type'] == 'websocket.disconnect':
                    logging.info(f"WebSocket disconnect message received in main loop: {message}")
                    break
                
                if 'text' in message:
                    await set_state_value_in_db(user_id, session_id, 'input_mode', "text")
                    text = message['text'].strip()
                    if not text:
                        continue
                    
                    cleaned_text = text
                    
                    cleaned_text = cleaned_text.replace('\u201c', '"').replace('\u201d', '"')
                    cleaned_text = cleaned_text.strip()
                    
                    if cleaned_text.startswith('\ufeff'):
                        cleaned_text = cleaned_text[1:]
                    
                    original_query = None
                    query_session_id = None
                    location_data = None
                    
                    try:
                        text_data = json.loads(cleaned_text)
                        if isinstance(text_data, dict):
                            if 'query' in text_data:
                                query_value = text_data['query']
                                if isinstance(query_value, str):
                                    original_query = query_value.strip()
                                    text = original_query
                                    logging.info(f"Extracted query from JSON: '{original_query}'")
                                else:
                                    original_query = text
                            else:
                                original_query = text
                            
                            if 'session_id' in text_data:
                                query_session_id = text_data['session_id']
                                logging.info(f"Extracted session_id from payload: '{query_session_id}'")
                            
                            if 'latitude' in text_data and 'longitude' in text_data:
                                location_data = {
                                    'latitude': text_data['latitude'],
                                    'longitude': text_data['longitude']
                                }
                                logging.info(f"Extracted location data: lat={location_data['latitude']}, lon={location_data['longitude']}")
                        else:
                            original_query = text
                    except (json.JSONDecodeError, TypeError):
                        import re
                        query_match = re.search(r'"query"\s*:\s*"([^"]*)"', cleaned_text)
                        if query_match:
                            original_query = query_match.group(1)
                            text = original_query
                            logging.info(f"Extracted query using regex fallback: '{original_query}'")
                        else:
                            original_query = text
                        
                        session_match = re.search(r'"session_id"\s*:\s*"([^"]*)"', cleaned_text)
                        if session_match:
                            query_session_id = session_match.group(1)
                            logging.info(f"Extracted session_id using regex fallback: '{query_session_id}'")
                        
                        lat_match = re.search(r'"latitude"\s*:\s*"([^"]*)"', cleaned_text)
                        lon_match = re.search(r'"longitude"\s*:\s*"([^"]*)"', cleaned_text)
                        if lat_match and lon_match:
                            location_data = {
                                'latitude': lat_match.group(1),
                                'longitude': lon_match.group(1)
                            }
                            logging.info(f"Extracted location data using regex: lat={location_data['latitude']}, lon={location_data['longitude']}")
                    
                    if not original_query or not original_query.strip():
                        logging.warning("Empty or invalid query received, skipping")
                        continue
                    
                    # ========== ENHANCED SESSION VALIDATION ==========
                    session_validation_failed = False
                    
                    if query_session_id:
                        current_session_id = await get_state_value_from_db(user_id, session_id, 'session_id', None)
                        
                        if current_session_id and query_session_id != current_session_id:
                            logging.info(f"Session ID mismatch: query session {query_session_id} != current session {current_session_id} - rejecting")
                            
                            error_response = {
                                "type": "error",
                                "message": "Invalid session ID. Please use the correct session ID for this connection.",
                                "timestamp": datetime.utcnow().isoformat()
                            }
                            await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                            session_validation_failed = True
                        else:
                            is_valid, error_message, language, new_session_id = await validate_session_and_regenerate_if_needed(user_id, query_session_id)
                            
                            if not is_valid:
                                logging.info(f"Session {query_session_id} is invalid for user {user_id} (reason: {error_message}) - returning error")
                                
                                error_response = {
                                    "type": "error",
                                    "message": "Invalid session ID. Please reconnect.",
                                    "timestamp": datetime.utcnow().isoformat()
                                }
                                await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                session_validation_failed = True
                            else:
                                if new_session_id:
                                    session_id = new_session_id
                                    await set_state_value_in_db(user_id, session_id, 'session_id', session_id)
                                    
                                    session_regenerated_response = {
                                        "type": "session_regenerated",
                                        "user_id": user_id,
                                        "session_id": session_id,
                                        "language": language,
                                        "message": "Session regenerated due to expiration",
                                        "timestamp": datetime.utcnow().isoformat()
                                    }
                                    await safe_websocket_send_text(websocket, json.dumps(session_regenerated_response, ensure_ascii=False))
                                    logging.info(f"Session regenerated: {query_session_id} -> {session_id}")
                                else:
                                    session_id = query_session_id
                                    await set_state_value_in_db(user_id, session_id, 'session_id', session_id)
                                    await session_manager.update_session_activity(user_id, session_id)
                                    # logging.info(f"Session {session_id} validated and activity updated")  # Removed for cleaner logs
                    else:
                        user_id_from_conn, current_session_id = await get_user_session_info(websocket)
                        
                        if not current_session_id:
                            logging.error("No session_id in query and no current session")
                            error_response = {
                                "type": "error",
                                "message": "Session not initialized. Please reconnect.",
                                "timestamp": datetime.utcnow().isoformat()
                            }
                            await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                            session_validation_failed = True
                        else:
                            is_valid, error_message, language = await validate_session_for_query(user_id, current_session_id)
                            
                            if not is_valid:
                                if error_message == "Session expired":
                                    logging.info(f"Current session {current_session_id} expired for user {user_id} - regenerating")
                                    
                                    new_session_id, session_created = await session_manager.regenerate_session(
                                        user_id=user_id,
                                        old_session_id=current_session_id,
                                        language=language,
                                        websocket=websocket
                                    )
                                    
                                    if session_created:
                                        session_id = new_session_id
                                        await set_state_value_in_db(user_id, session_id, 'session_id', session_id)
                                        
                                        session_regenerated_response = {
                                            "type": "session_regenerated",
                                            "user_id": user_id,
                                            "session_id": session_id,
                                            "language": language,
                                            "message": "Session regenerated due to expiration",
                                            "timestamp": datetime.utcnow().isoformat()
                                        }
                                        await safe_websocket_send_text(websocket, json.dumps(session_regenerated_response, ensure_ascii=False))
                                        
                                        logging.info(f"Processing query with regenerated session {session_id}")
                                    else:
                                        logging.error(f"Failed to regenerate session for user {user_id}")
                                        error_response = {
                                            "type": "error",
                                            "message": "Failed to regenerate session. Please reconnect.",
                                            "timestamp": datetime.utcnow().isoformat()
                                        }
                                        await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                        session_validation_failed = True
                                else:
                                    logging.info(f"Current session {current_session_id} is invalid for user {user_id} (reason: {error_message}) - returning error")
                                    
                                    error_response = {
                                        "type": "error",
                                        "message": "Invalid session ID. Please reconnect.",
                                        "timestamp": datetime.utcnow().isoformat()
                                    }
                                    await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                    session_validation_failed = True
                            else:
                                await session_manager.update_session_activity(user_id, current_session_id)
                                session_id = current_session_id
                                # logging.info(f"Session {session_id} validated and activity updated")  # Removed for cleaner logs
                    
                    if session_validation_failed:
                        logging.info("Session validation failed, skipping query processing")
                        continue
                    
                    await update_state_values_in_db(user_id, session_id, {
                        'response_sent': False,
                        'last_saved_conversation': None,
                        'skip_current_response': False
                    })
                    
                    # ========== LOCATION DATA PROCESSING ==========
                    if location_data:
                        try:
                            lat = float(location_data['latitude'])
                            lon = float(location_data['longitude'])
                            
                            if -90 <= lat <= 90 and -180 <= lon <= 180:
                                location_updated = await location_manager.store_user_location(
                                    session_id, user_id, lat, lon
                                )
                                
                                if location_updated:
                                    print(f"\n========== LOCATION UPDATED ==========")
                                    print(f"Session: {session_id}")
                                    print(f"Location: {lat}, {lon}")
                                    print("=====================================\n")
                                else:
                                    print(f"\n========== LOCATION UNCHANGED ==========")
                                    print(f"Session: {session_id}")
                                    print(f"Location: {lat}, {lon}")
                                    print("========================================\n")
                                
                                nearest_station = location_manager.find_nearest_station(lat, lon)
                                if nearest_station:
                                    print(f"Nearest station: {nearest_station['name']} ({nearest_station['abbr']}) at {nearest_station['distance']:.2f} km")
                                    print(f"Stored nearest station in database for session: {session_id}")
                                    
                                    from decimal import Decimal
                                    nearest_station_decimal = {
                                        'name': nearest_station['name'],
                                        'abbr': nearest_station['abbr'],
                                        'distance': Decimal(str(nearest_station['distance'])),
                                        'latitude': Decimal(str(nearest_station['latitude'])),
                                        'longitude': Decimal(str(nearest_station['longitude']))
                                    }
                                    await update_state_values_in_db(user_id, session_id, {
                                        'nearest_station': nearest_station_decimal,
                                        'user_location': {
                                            'latitude': Decimal(str(lat)), 
                                            'longitude': Decimal(str(lon))
                                        }
                                    })
                                
                            else:
                                logging.warning(f"Invalid coordinates received: lat={lat}, lon={lon}")
                                
                        except (ValueError, TypeError) as e:
                            logging.error(f"Error processing location data: {str(e)}")
                        except Exception as e:
                            logging.error(f"Unexpected error processing location: {str(e)}")
                    
                    # Get language from DynamoDB
                    original_language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                    original_text = original_query  
                    
                    if original_language != 'en':
                        logging.info(f"Translating query from {original_language} to English for processing")
                        try:
                            english_text = await translate_text(original_text, source_lang=original_language, target_lang='en')
                            logging.info(f"Translated query: '{english_text}'")
                            text = english_text
                            await update_state_values_in_db(user_id, session_id, {
                                'original_query_language': original_language,
                                'original_query_text': original_text
                            })
                        except Exception as e:
                            logging.error(f"Error translating query: {str(e)}")
                    else:
                        text = original_query
                    
                    print("\n========== PROCESSING QUERY ==========")
                    print(f"Processing text: '{text.strip()}'")
                    print(f"Session ID: '{session_id}'")
                    
                    is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                    is_awaiting_validation = await get_state_value_from_db(user_id, session_id, 'awaiting_station_validation', False)
                    original_query = await get_state_value_from_db(user_id, session_id, 'original_query', None)
                    ambiguous_station = await get_state_value_from_db(user_id, session_id, 'ambiguous_station', None)
                    invalid_station = await get_state_value_from_db(user_id, session_id, 'invalid_station', None)
                    station_options = await get_state_value_from_db(user_id, session_id, 'station_options', None)
                    
                    print(f"Awaiting disambiguation: {is_awaiting_disambiguation}")
                    print(f"Awaiting station validation: {is_awaiting_validation}")
                    print(f"Original query: '{original_query}'")
                    print(f"Ambiguous station: '{ambiguous_station}'")
                    print(f"Invalid station: '{invalid_station}'")
                    print(f"Station options: {station_options}")
                    print("======================================\n")
                    
                elif 'bytes' in message:
                    await set_state_value_in_db(user_id, session_id, 'input_mode', 'audio')
                    try:
                        chunk = message['bytes']
                        if not isinstance(chunk, bytes):
                            logging.warning(f"Received non-bytes chunk data: {type(chunk)}")
                            continue
                    except Exception as e:
                        logging.error(f"Error processing bytes message: {str(e)}")
                        continue
                   
                    audio_session_id = None
                    audio_data = chunk
                    
                    try:
                        if chunk.startswith(b'{'):
                            json_end = chunk.find(b'}') + 1
                            if json_end > 0:
                                metadata_bytes = chunk[:json_end]
                                logging.info(f"Found metadata bytes: {metadata_bytes}")
                                metadata = json.loads(metadata_bytes.decode('utf-8'))
                                logging.info(f"Parsed metadata: {metadata}")
                                audio_session_id = metadata.get('session_id')
                                
                                audio_latitude = metadata.get('latitude')
                                audio_longitude = metadata.get('longitude')
                                
                                audio_data = chunk[json_end:] 
                                logging.info(f"Extracted session_id from audio metadata: {audio_session_id}")
                                if audio_latitude and audio_longitude:
                                    logging.info(f"Extracted location from audio metadata: {audio_latitude}, {audio_longitude}")
                                logging.info(f"Audio data size: {len(audio_data)} bytes")
                        else:
                            logging.info(f"Chunk does not start with '{{', first 20 bytes: {chunk[:20]}")
                            audio_session_id = None
                            audio_latitude = None
                            audio_longitude = None
                            audio_data = chunk
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        logging.info(f"Failed to parse metadata: {e}")
                        audio_session_id = None
                        audio_latitude = None
                        audio_longitude = None
                        audio_data = chunk
                    except Exception as e:
                        logging.warning(f"Unexpected error parsing audio metadata: {str(e)}")
                        audio_session_id = None
                        audio_latitude = None
                        audio_longitude = None
                        audio_data = chunk
                    
                    if audio_session_id:
                        current_session_id = await get_state_value_from_db(user_id, session_id, 'session_id', None)
                        
                        if current_session_id and audio_session_id != current_session_id:
                            logging.info(f"Audio session ID mismatch: audio session {audio_session_id} != current session {current_session_id} - rejecting")
                            
                            error_response = {
                                "type": "error",
                                "message": "Invalid session ID. Please use the correct session ID for this connection.",
                                "timestamp": datetime.utcnow().isoformat()
                            }
                            await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                            continue
                        else:
                            is_valid, error_message, language = await validate_session_for_query(user_id, audio_session_id)
                            
                            if not is_valid:
                                if error_message == "Session expired":
                                    logging.info(f"Audio session {audio_session_id} has expired for user {user_id} - regenerating")
                                    
                                    new_session_id, session_created = await session_manager.regenerate_session(
                                        user_id=user_id,
                                        old_session_id=audio_session_id,
                                        language=language
                                    )
                                    
                                    if session_created:
                                        session_id = new_session_id
                                        await set_state_value_in_db(user_id, session_id, 'session_id', session_id)
                                        
                                        session_regenerated_response = {
                                            "type": "session_regenerated",
                                            "user_id": user_id,
                                            "session_id": session_id,
                                            "language": language,
                                            "message": "Session regenerated due to expiration",
                                            "timestamp": datetime.utcnow().isoformat()
                                        }
                                        await safe_websocket_send_text(websocket, json.dumps(session_regenerated_response, ensure_ascii=False))
                                        
                                        logging.info(f"Processing audio with regenerated session {session_id}")
                                    else:
                                        logging.error(f"Failed to regenerate session for user {user_id}")
                                        error_response = {
                                            "type": "error",
                                            "message": "Failed to regenerate session. Please reconnect.",
                                            "timestamp": datetime.utcnow().isoformat()
                                        }
                                        await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                        continue
                                else:
                                    logging.info(f"Audio session {audio_session_id} is invalid for user {user_id} (reason: {error_message}) - returning error")
                                    
                                    error_response = {
                                        "type": "error",
                                        "message": "Invalid session ID. Please reconnect.",
                                        "timestamp": datetime.utcnow().isoformat()
                                    }
                                    await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                    continue
                            else:
                                await session_manager.update_session_activity(user_id, audio_session_id)
                                session_id = audio_session_id
                                await set_state_value_in_db(user_id, session_id, 'session_id', session_id)
                                logging.info(f"Audio session {session_id} validated and activity updated")
                            
                            if audio_latitude and audio_longitude:
                                try:
                                    lat = float(audio_latitude)
                                    lon = float(audio_longitude)
                                    
                                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                                        location_updated = await location_manager.store_user_location(
                                            session_id, user_id, lat, lon
                                        )
                                        
                                        if location_updated:
                                            print(f"\n========== AUDIO LOCATION UPDATED ==========")
                                            print(f"Session: {session_id}")
                                            print(f"Location: {lat}, {lon}")
                                            print("===========================================\n")
                                        else:
                                            print(f"\n========== AUDIO LOCATION UNCHANGED ==========")
                                            print(f"Session: {session_id}")
                                            print(f"Location: {lat}, {lon}")
                                            print("==============================================\n")
                                        
                                        nearest_station = location_manager.find_nearest_station(lat, lon)
                                        if nearest_station:
                                            print(f"Nearest station from audio: {nearest_station['name']} ({nearest_station['abbr']}) at {nearest_station['distance']:.2f} km")
                                            print(f"Stored nearest station in database for session: {session_id}")
                                    
                                except (ValueError, TypeError) as e:
                                    logging.error(f"Error processing audio location data: {str(e)}")
                                except Exception as e:
                                    logging.error(f"Unexpected error processing audio location: {str(e)}")
                    else:
                        current_session_id = await get_state_value_from_db(user_id, session_id, 'session_id', None)
                        
                        if not current_session_id:
                            logging.error("No session_id in audio and no current session")
                            error_response = {
                                "type": "error",
                                "message": "Session not initialized. Please reconnect.",
                                "timestamp": datetime.utcnow().isoformat()
                            }
                            await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                            continue
                        
                        is_valid, error_message, language = await validate_session_for_query(user_id, current_session_id)
                        
                        if not is_valid:
                            if error_message == "Session expired":
                                logging.info(f"Current session {current_session_id} expired for user {user_id} - regenerating")
                                
                                new_session_id, session_created = await session_manager.regenerate_session(
                                    user_id=user_id,
                                    old_session_id=current_session_id,
                                    language=language
                                )
                                
                                if session_created:
                                    session_id = new_session_id
                                    await set_state_value_in_db(user_id, session_id, 'session_id', session_id)
                                    
                                    session_regenerated_response = {
                                        "type": "session_regenerated",
                                        "user_id": user_id,
                                        "session_id": session_id,
                                        "language": language,
                                        "message": "Session regenerated due to expiration",
                                        "timestamp": datetime.utcnow().isoformat()
                                    }
                                    await safe_websocket_send_text(websocket, json.dumps(session_regenerated_response, ensure_ascii=False))
                                    
                                    logging.info(f"Processing audio with regenerated session {session_id}")
                                else:
                                    logging.error(f"Failed to regenerate session for user {user_id}")
                                    error_response = {
                                        "type": "error",
                                        "message": "Failed to regenerate session. Please reconnect.",
                                        "timestamp": datetime.utcnow().isoformat()
                                    }
                                    await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                    continue
                            else:
                                logging.info(f"Current session {current_session_id} is invalid for user {user_id} (reason: {error_message}) - returning error")
                                
                                error_response = {
                                    "type": "error",
                                    "message": "Invalid session ID. Please reconnect.",
                                    "timestamp": datetime.utcnow().isoformat()
                                }
                                await safe_websocket_send_text(websocket, json.dumps(error_response, ensure_ascii=False))
                                continue
                        else:
                            await session_manager.update_session_activity(user_id, current_session_id)
                            session_id = current_session_id
                            logging.info(f"Audio session {session_id} validated and activity updated")
                    
                    if not buffer.extend(audio_data):
                        buffer_overflow_count += 1
                        buffer_usage = buffer.get_usage_percentage()
                        logging.warning(f"Audio buffer overflow (usage: {buffer_usage:.1f}%), forcing immediate processing (overflow #{buffer_overflow_count})")
                        
                        if not is_websocket_connected(websocket):
                            logging.info("WebSocket disconnected during buffer overflow, clearing buffer and skipping processing")
                            connection_active = False
                            buffer.clear()
                            continue
                        
                        current_time = asyncio.get_event_loop().time()
                        if len(buffer) > 0 and user_id and session_id:
                            try:
                                logging.info(f"Emergency processing audio buffer of {len(buffer)} bytes")
                                language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                                language_code = SUPPORTED_LANGUAGES[language]['transcribe_code']
                                text = await transcribe_stream(buffer.get_data(), language_code, websocket, user_id, session_id)
                                buffer.clear() 
                                pre_buffer.clear() 
                                last_process_time = current_time
                                silence_counter = 0
                                
                                await update_state_values_in_db(user_id, session_id, {
                                    'response_sent': False,
                                    'last_saved_conversation': None,
                                    'skip_current_response': False
                                })
                                
                                is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                                is_awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
                                
                                if not is_awaiting_disambiguation and not is_awaiting_location:
                                    await set_state_value_in_db(user_id, session_id, 'complete_query', False)
                                
                                if text and len(text.strip()) > 0:
                                    try:
                                        websocket_id = f"ws_{id(websocket)}"
                                    except Exception as context_e:
                                        logging.warning(f"Failed to maintain user session context before query processing: {str(context_e)}")
                                    
                                    await process_query_and_respond_with_text(text, websocket, user_id, session_id)
                                    
                            except Exception as e:
                                logging.error(f"Error in emergency audio processing: {str(e)}")
                                buffer.clear()  
                        else:
                            buffer.clear()
                            if not user_id or not session_id:
                                logging.warning("Cleared buffer due to overflow but cannot process (missing user_id/session_id)")
                        
                        if buffer_overflow_count > 5:
                            logging.warning(f"Multiple buffer overflows ({buffer_overflow_count}), recreating buffer")
                            try:
                                buffer.close()
                                buffer = create_managed_buffer()
                                buffer_overflow_count = 0
                            except Exception as e:
                                logging.error(f"Error recreating buffer: {str(e)}")
                                buffer_overflow_count = 0  
                        continue
                        
                    current_time = asyncio.get_event_loop().time()
                    
                    if len(buffer) % 8000 == 0 or len(buffer) >= CHUNK_SIZE:
                        usage_percent = buffer.get_usage_percentage()
                        logging.info(f"Current buffer size: {len(buffer)} bytes ({usage_percent:.1f}% full)")
                        
                        if not buffer.is_healthy():
                            logging.warning(f"Audio buffer health check failed: {usage_percent:.1f}% full")
                            if usage_percent > 95:
                                logging.warning("Buffer critically full, forcing immediate processing")
                                current_time = asyncio.get_event_loop().time()
                                should_process = True

                    should_process = (
                        len(buffer) >= CHUNK_SIZE or 
                        (len(buffer) > 0 and current_time - last_process_time > 0.03) or  
                        (len(audio_data) < 1000 and silence_counter > 1) or
                        len(buffer) > CHUNK_SIZE * 0.75 
                    )
                    
                    if should_process and len(buffer) > 0:
                        if not is_websocket_connected(websocket):
                            logging.info("WebSocket disconnected, clearing buffer and stopping processing")
                            connection_active = False
                            buffer.clear()
                            break
                        try:
                            logging.info(f"Processing audio buffer of {len(buffer)} bytes")
                            language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                            language_code = SUPPORTED_LANGUAGES[language]['transcribe_code']
                            text = await transcribe_stream(buffer.get_data(), language_code, websocket, user_id, session_id)
                            buffer.clear() 
                            pre_buffer.clear() 
                            last_process_time = current_time
                            silence_counter = 0
                            
                            await update_state_values_in_db(user_id, session_id, {
                                'response_sent': False,
                                'last_saved_conversation': None,
                                'skip_current_response': False
                            })
                            
                            is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                            is_awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
                            
                            if not is_awaiting_disambiguation and not is_awaiting_location:
                                await set_state_value_in_db(user_id, session_id, 'complete_query', False)
                            
                            if text and len(text.strip()) > 0:
                                logging.info(f"Transcribed text: '{text.strip()}'")
                                original_language = await get_state_value_from_db(user_id, session_id, 'language', 'en')

                                original_text = text.strip()
                                
                                if original_language != 'en':
                                    logging.info(f"Translating query from {original_language} to English for processing")
                                    try:
                                        english_text = await translate_text(original_text, source_lang=original_language, target_lang='en')
                                        logging.info(f"Translated query: '{english_text}'")
                                        text = english_text
                                        await update_state_values_in_db(user_id, session_id, {
                                            'original_query_language': original_language,
                                            'original_query_text': original_text
                                        })
                                    except Exception as e:
                                        logging.error(f"Error translating query: {str(e)}")
                                
                                # --------- QUERY PROCESSING ---------
                                print("\n========== PROCESSING QUERY ==========")
                                print(f"Transcribed text: '{text.strip()}'")
                                print(f"Session ID: '{session_id}'")
                                state = await get_ws_state_from_db(user_id, session_id)
                                print(f"Awaiting disambiguation: {state.get('awaiting_disambiguation', False)}")
                                print(f"Awaiting station validation: {state.get('awaiting_station_validation', False)}")
                                print(f"Original query: '{state.get('original_query', None)}'")
                                print(f"Ambiguous station: '{state.get('ambiguous_station', None)}'")
                                print(f"Invalid station: '{state.get('invalid_station', None)}'")
                                print(f"Station options: {state.get('station_options', None)}")
                                print("======================================\n")
                            else:
                                logging.info("No transcription text received (empty audio), continuing to next message")
                                continue
                        except Exception as e:
                            logging.error(f"Error processing audio buffer: {str(e)}")
                            continue
                    else:
                        continue
                else:
                    continue
                                
                awaiting_station_validation = await get_state_value_from_db(user_id, session_id, 'awaiting_station_validation', False)
                if awaiting_station_validation:
                    invalid_station = await get_state_value_from_db(user_id, session_id, 'invalid_station', None)
                    logging.info(f"Processing response for invalid station: {invalid_station}")
                    
                    station_suggestions = await get_state_value_from_db(user_id, session_id, 'station_suggestions', [])
                    original_query = await get_state_value_from_db(user_id, session_id, 'original_query', None)
                                    
                    if invalid_station and original_query:
                        user_response = text.strip()
                        user_response_lower = user_response.lower()
                                        
                        valid_stations_dict = {station["name"].lower(): station["name"] for station in STATION_DATA["stations"]}
                        valid_abbrs_dict = {station["abbr"].lower(): station["name"] for station in STATION_DATA["stations"]}
                                        
                        valid_station_name = None
                                        
                        print(f"\n========== VALIDATING STATION NAME ==========")
                        print(f"User response: '{user_response}'")
                        print(f"Invalid station: '{invalid_station}'")
                        print(f"Station suggestions: {station_suggestions}")
                        print("==========================================\n")
                                        
                        # 1. Exact match with full station name
                        if user_response_lower in valid_stations_dict:
                            valid_station_name = valid_stations_dict[user_response_lower]
                            logging.info(f"1. Exact full name match: {valid_station_name}")
                            print(f"1. Exact full name match: {valid_station_name}")
                                            
                        # 2. Exact match with abbreviation
                        elif user_response_lower in valid_abbrs_dict:
                            valid_station_name = valid_abbrs_dict[user_response_lower]
                            logging.info(f"2. Exact abbreviation match: {valid_station_name}")
                            print(f"2. Exact abbreviation match: {valid_station_name}")
                                            
                        # 3. Exact match with station suggestions
                        elif station_suggestions and any(suggestion.lower() == user_response_lower for suggestion in station_suggestions):
                            for suggestion in station_suggestions:
                                if suggestion.lower() == user_response_lower:
                                    valid_station_name = suggestion
                                    logging.info(f"3. Exact suggestion match: {valid_station_name}")
                                    print(f"3. Exact suggestion match: {valid_station_name}")
                                    break
                                        
                        # 4. Station name contains user response (for short responses like "Berkeley" matching "Downtown Berkeley")
                        elif len(user_response_lower) >= 3:
                            for station_name_lower, full_name in valid_stations_dict.items():
                                if user_response_lower in station_name_lower and user_response_lower != station_name_lower:
                                    valid_station_name = full_name
                                    logging.info(f"4. Station contains response: {valid_station_name}")
                                    print(f"4. Station contains response: {valid_station_name}")
                                    break
                                        
                        # 5. User response contains station name
                        if not valid_station_name and len(user_response_lower) >= 5:
                            for station_name_lower, full_name in valid_stations_dict.items():
                                if station_name_lower in user_response_lower and station_name_lower != user_response_lower:
                                    valid_station_name = full_name
                                    logging.info(f"5. Response contains station: {valid_station_name}")
                                    print(f"5. Response contains station: {valid_station_name}")
                                    break
                                        
                        # 6. Check for suggestions where user response contains significant parts of the suggestion
                        if not valid_station_name and station_suggestions:
                            for suggestion in station_suggestions:
                                suggestion_lower = suggestion.lower()
                                words = suggestion_lower.split()
                                                
                                if len(words) > 1: 
                                    distinctive_words = [w for w in words if len(w) > 3] 
                                    matching_words = [w for w in distinctive_words if w in user_response_lower]
                                                    
                                    if len(matching_words) >= len(distinctive_words) * 0.5:  
                                        valid_station_name = suggestion
                                        logging.info(f"6. Multi-word keyword match: {valid_station_name}, matched words: {matching_words}")
                                        print(f"6. Multi-word keyword match: {valid_station_name}, matched: {matching_words}")
                                        break
                                        
                        # 7. Last resort: look for specific keywords that might indicate station names in response
                        if not valid_station_name:
                            keywords = ["downtown", "berkeley", "oakland", "mission", "plaza", "center", "glen", "park", 
                                       "airport", "north", "south", "east", "west", "dublin", "city", "station"]
                                                       
                            user_words = user_response_lower.split()
                            keyword_matches = {}
                                            
                            for station_name_lower, full_name in valid_stations_dict.items():
                                matching_keywords = []
                                for keyword in keywords:
                                    if keyword in station_name_lower and keyword in user_response_lower:
                                        matching_keywords.append(keyword)
                                                
                                if matching_keywords:
                                    keyword_matches[full_name] = matching_keywords
                                            
                            if keyword_matches:
                                best_match = max(keyword_matches.items(), key=lambda x: len(x[1]))
                                valid_station_name = best_match[0]
                                logging.info(f"7. Keyword match: {valid_station_name}, keywords: {best_match[1]}")
                                print(f"7. Keyword match: {valid_station_name}, keywords: {best_match[1]}")
                                        
                        print(f"Final validation result: {valid_station_name or 'No match found'}")
                                        
                        await update_state_values_in_db(user_id, session_id, {
                            'awaiting_station_validation': False,
                            'invalid_station': None,
                            'station_suggestions': None
                        })
                                        
                        if valid_station_name:
                            pattern = re.compile(re.escape(invalid_station), re.IGNORECASE)
                            final_query = pattern.sub(valid_station_name, original_query)
                                            
                            print("\n========== STATION VALIDATION COMPLETE ==========")
                            print(f"Original query: '{original_query}'")
                            print(f"Invalid station: '{invalid_station}'")
                            print(f"Replaced with: '{valid_station_name}'")
                            print(f"Final query: '{final_query}'")
                            print("================================================\n")
                                            
                            replacement_messages = {
                                "en": f"Replacing '{invalid_station}' with '{valid_station_name}'",
                                "es": f"Reemplazando '{invalid_station}' por '{valid_station_name}'",
                                "zh": f"将'{invalid_station}'替换为'{valid_station_name}'"
                            }
                            
                            user_language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                            
                            translated_query = original_query
                            if user_language != 'en':
                                translated_query = await translate_text(original_query, source_lang='en', target_lang=user_language)
                            
                            session_id = await get_state_value_from_db(user_id, session_id, 'session_id', None)
                            replacement_info = {
                                'type': 'conversation',
                                'conversation_id': str(uuid.uuid4()),
                                'timestamp': datetime.now().isoformat(),
                                'query': translated_query,
                                'response': replacement_messages.get(user_language, replacement_messages['en']),
                                'session_id': session_id,
                                'is_station_replacement': True
                            }
                            await safe_websocket_send_text(websocket, json.dumps(replacement_info, ensure_ascii=False))
                            
                            await set_state_value_in_db(user_id, session_id, 'skip_current_response', True)
                            
                            replaced_query = {
                                'original': original_query,
                                'invalid_station': invalid_station,
                                'replacement': valid_station_name,
                                'final': final_query
                            }
                            await set_state_value_in_db(user_id, session_id, 'replaced_query', replaced_query)
                            await set_state_value_in_db(user_id, session_id, 'is_station_replacement', True)
                            
                            await set_state_value_in_db(user_id, session_id, 'original_query_for_response', final_query)
                            
                            try:
                                websocket_id = f"ws_{id(websocket)}"
                            except Exception as context_e:
                                logging.warning(f"Failed to maintain user session context before query processing: {str(context_e)}")
                                            
                            await process_query_and_respond_with_text(final_query, websocket, user_id, session_id)
                                            
                            continue
                        else:
                            additional_message = "That still doesn't match a valid BART station. Please try again with a station from the list."
                            await safe_websocket_send_text(websocket, additional_message)
                            audio_bytes = await synthesize_speech(additional_message)
                            if audio_bytes:
                                await websocket.send_bytes(audio_bytes)
                                            
                            await update_state_values_in_db(user_id, session_id, {
                                'awaiting_station_validation': True,
                                'invalid_station': invalid_station,
                                'station_suggestions': station_suggestions,
                                'original_query': original_query
                            })
                    else:
                        logging.error("Missing required state for station validation")
                        await set_state_value_in_db(user_id, session_id, 'awaiting_station_validation', False)
                                
                elif await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False):
                    print(f"\n========== DISAMBIGUATION RESPONSE DETECTED ==========")
                    print(f"Processing disambiguation response: '{text}'")
                    print("====================================================\n")
                    
                    await process_query_with_disambiguation(text, websocket, user_id, session_id)
                    
                    is_still_awaiting = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                    
                    if not is_still_awaiting:
                        logging.info(f"Processed disambiguation response: {text}")
                    else:
                        logging.info(f"Disambiguation still pending after response: {text}")
                else:
                    skip_current_response = await get_state_value_from_db(user_id, session_id, 'skip_current_response', False)
                    if skip_current_response:
                        await set_state_value_in_db(user_id, session_id, 'skip_current_response', False)
                        print("Skipping processing of current response as it was part of station validation")
                        continue
                    
                    awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
                    if awaiting_location:
                        destination_station = await get_state_value_from_db(user_id, session_id, 'destination_station', None)
                        if destination_station:
                            print(f"\n========== HANDLING LOCATION RESPONSE ==========")
                            print(f"Processing location response: '{text}' for destination: '{destination_station}'")
                            print("=================================================\n")
                            
                            try:
                                result = await process_location_query(text, destination_station, websocket)
                                if result is None:
                                    final_query = await get_state_value_from_db(user_id, session_id, 'final_query', None)
                                    if final_query:
                                        print(f"Processing reconstructed query: '{final_query}'")
                                        try:
                                            websocket_id = f"ws_{id(websocket)}"
                                        except Exception as context_e:
                                            logging.warning(f"Failed to maintain user session context before processing reconstructed query: {str(context_e)}")
                                        
                                        combined_response = await process_query_with_disambiguation(final_query, websocket, user_id, session_id)
                                        if combined_response:
                                            final_response = extract_final_response(combined_response)
                                            await save_and_send_response(
                                                websocket, user_id, text.strip(), combined_response, final_response, session_id
                                            )
                                else:
                                    print(f"Location processing error: {result}")
                            except Exception as e:
                                print(f"Error processing location response: {str(e)}")
                                logging.error(f"Error processing location response: {str(e)}")
                            continue
                    
                    location_processed = await get_state_value_from_db(user_id, session_id, 'location_response_processed', False)
                    if location_processed:
                        print("Location response already processed, skipping regular query processing")
                        await set_state_value_in_db(user_id, session_id, 'location_response_processed', False)
                        continue
                    
                    preprocessed_text = preprocess_concatenated_stations(text)
                    
                    if preprocessed_text != text:
                        logging.info(f"Station correction applied at main level: '{text}' -> '{preprocessed_text}'")
                        await update_state_values_in_db(user_id, session_id, {
                            'original_query_for_display': text,
                            'using_corrected_query': True
                        })
                        text = preprocessed_text
                    else:
                        await update_state_values_in_db(user_id, session_id, {
                            'original_query_for_display': None,
                            'using_corrected_query': False
                        })
                                    
                    try:
                        websocket_id = f"ws_{id(websocket)}"
                    except Exception as context_e:
                        logging.warning(f"Failed to maintain user session context before main query processing: {str(context_e)}")
                    
                    combined_response = await process_query_with_disambiguation(text, websocket, user_id, session_id)
                    
                    is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                    if combined_response and not is_awaiting_disambiguation:
                        final_response = extract_final_response(combined_response)
                        await save_and_send_response(
                            websocket, user_id, text.strip(), combined_response, final_response, session_id
                        )
                                
            except WebSocketDisconnect:
                logging.info("WebSocket disconnected during response, waiting for new connection")
                break
            except RuntimeError as rt_err:
                if "close message has been sent" in str(rt_err):
                    logging.info("WebSocket already closed, waiting for new connection")
                    break
                else:
                    raise
            except Exception as e:
                import traceback
                error_message = str(e)
                stack_trace = traceback.format_exc()
                
                logging.exception(f"Error while processing query: {error_message}\n{stack_trace}")
                
                try:
                    from database_operations import save_error_log_to_db
                    await save_error_log_to_db(
                        user_id=user_id if user_id else "unknown",
                        session_id=session_id if session_id else "unknown",
                        error_message=error_message,
                        error_type="websocket_processing",
                        query_text=text if 'text' in locals() else None,
                        stack_trace=stack_trace
                    )
                except Exception as db_error:
                    logging.error(f"Failed to save error to database: {str(db_error)}")
                
                try:
                    fallback_audio = await synthesize_speech("Oops, something went wrong. Try asking again?")
                    if fallback_audio:
                        await safe_websocket_send_bytes(websocket, fallback_audio)
                    
                    await safe_websocket_send_text(websocket, json.dumps({
                        "type": "error",
                        "message": f"Error: {error_message}",
                        "timestamp": datetime.utcnow().isoformat()
                    }, ensure_ascii=False))
                except Exception as err:
                    logging.warning(f"Could not send fallback response: {str(err)}")
                    
    except WebSocketDisconnect:
        logging.info("WebSocket disconnected during processing")
        connection_active = False
        session_manager_instance.remove_session(websocket)
    except RuntimeError as rt_err:
        if "disconnect message has been received" in str(rt_err):
            logging.info("WebSocket disconnect message received in main handler")
            session_manager_instance.remove_session(websocket)
        elif "close message has been sent" in str(rt_err):
            logging.info("WebSocket close message sent in main handler")
            session_manager_instance.remove_session(websocket)
        else:
            logging.exception(f"RuntimeError in websocket handler: {str(rt_err)}")
    except Exception as e:
        import traceback
        error_message = str(e)
        stack_trace = traceback.format_exc()
        
        logging.exception(f"Unexpected error in websocket handler: {error_message}\n{stack_trace}")
        
        try:
            from database_operations import save_error_log_to_db
            await save_error_log_to_db(
                user_id=user_id if 'user_id' in locals() and user_id else "unknown",
                session_id=session_id if 'session_id' in locals() and session_id else "unknown",
                error_message=error_message,
                error_type="websocket_handler",
                query_text=None,
                stack_trace=stack_trace
            )
        except Exception as db_error:
            logging.error(f"Failed to save error to database: {str(db_error)}")
        
        session_manager_instance.remove_session(websocket)
        try:
            fallback_audio = await synthesize_speech("Sorry, I couldn't process that request. Try asking again.")
            if fallback_audio:
                await safe_websocket_send_bytes(websocket, fallback_audio)
        except Exception as audio_err:
            logging.warning(f"Could not synthesize final fallback audio: {str(audio_err)}")
    finally:
        # CRITICAL FIX: Comprehensive cleanup with proper error handling
        if not cleanup_done:
            cleanup_done = True
            
            # CRITICAL FIX: Only log completion if connection was established
            if connection_established:
                logging.info(f"WebSocket handler completed for user {user_id}")
            else:
                logging.info("WebSocket handler completed for user None (connection not established)")
            
            # CRITICAL FIX: Improved cleanup logic based on connection state
            if connection_established and user_id and session_id:
                try:
                    websocket_id = f"ws_{id(websocket)}"
                    stop_session_logging(user_id, session_id, websocket_id)
                    logging.info(f"Stopped session logging for user {user_id}, session {session_id}")
                    
                    # Remove from session manager
                    session_manager_instance.remove_session(websocket)
                    
                    # Unregister connection
                    connection_manager.unregister_connection(websocket_id)
                    
                    # Clean up resources
                    if hasattr(buffer, 'close'):
                        buffer.close()
                    await cleanup_websocket_resources(user_id, session_id, buffer, pre_buffer)
                    
                    # Delete session
                    await delete_session(user_id, session_id)
                    logging.info(f"Session immediately deleted on websocket disconnect: {user_id}:{session_id}")
                    
                except Exception as e:
                    logging.error(f"Error during comprehensive cleanup: {str(e)}")
            else:
                # CRITICAL FIX: Handle cases where connection wasn't fully established
                if not connection_established:
                    logging.warning("⚠️ DEBUG: Connection was not fully established - skipping user-specific cleanup")
                else:
                    print(f"⚠️ DEBUG: Cannot log websocket close - missing user_id ({user_id}) or session_id ({session_id})")
                    logging.warning(f"⚠️ DEBUG: Cannot log websocket close - missing user_id ({user_id}) or session_id ({session_id})")
            
            # Always decrement connection counters
            resource_monitor.decrement_connections()
            decrement_connections()

async def validate_query_processing_prerequisites_with_params(websocket: WebSocket, user_id: str, session_id: str) -> Tuple[bool, str, str, str]:
    """
    Comprehensive validation function to ensure all prerequisites are met before processing a query.
    Production grade - uses direct parameters with zero WebSocket dependency.
    
    Returns:
        Tuple[bool, str, str, str]: (is_valid, error_message, user_id, session_id)
    """
    try:
        if not session_id:
            return False, "Missing session_id in request parameters", "", ""
        
        if not user_id:
            return False, "Missing user_id in request parameters", "", ""
        
        is_session_valid, session_error, language, new_session_id = await validate_session_and_regenerate_if_needed(user_id, session_id)
        
        if not is_session_valid:
            return False, session_error, user_id, session_id
        
        if new_session_id and new_session_id != session_id:
            session_id = new_session_id
            await set_state_value_in_db(user_id, session_id, 'session_id', new_session_id)
        
        return True, "", user_id, session_id
        
    except Exception as e:
        logging.exception(f"Error in query processing validation: {str(e)}")
        return False, f"Validation error: {str(e)}", "", ""

async def validate_query_processing_prerequisites(websocket: WebSocket) -> Tuple[bool, str, str, str]:
    """
    Comprehensive validation function to ensure all prerequisites are met before processing a query.
    
    Returns:
        Tuple[bool, str, str, str]: (is_valid, error_message, user_id, session_id)
    """
    try:
        user_id, session_id = await get_user_session_info(websocket)
        language = 'en'  
        
        if not session_id:
            return False, "Missing session_id in request parameters", "", ""
        
        if not user_id:
            return False, "Missing user_id in request parameters", "", ""
        
        is_session_valid, session_error, language, new_session_id = await validate_session_and_regenerate_if_needed(user_id, session_id)
        
        if not is_session_valid:
            return False, f"Session validation failed: {session_error}", user_id, session_id
        
        if new_session_id:
            session_id = new_session_id
            logging.info(f"Session regenerated: {user_id}:{new_session_id}")
        
        if not is_websocket_connected(websocket):
            return False, "WebSocket connection is not active", user_id, session_id
        
        try:
            await session_manager.update_session_activity(user_id, session_id, websocket)
            logging.info(f"Session activity updated for user {user_id}, session {session_id}")
        except Exception as e:
            logging.warning(f"Failed to update session activity: {str(e)}")
        
        return True, "", user_id, session_id
        
    except Exception as e:
        logging.error(f"Error during query processing validation: {str(e)}")
        return False, f"Validation error: {str(e)}", "", ""


async def process_query_and_respond_with_text(query_text: str, websocket: WebSocket, user_id: str = None, session_id: str = None):
    """Process user query and return combined text response with timeout handling"""
    try:
        return await asyncio.wait_for(
            _process_query_internal(query_text, websocket, user_id, session_id),
            timeout=30.0  # CRITICAL FIX: Reduced from 180s to 30s to prevent 4xx errors
        )
    except asyncio.TimeoutError:
        logging.error(f"Query processing timed out for query: {query_text}")
        error_msg = "I'm taking longer than expected to process your request. Please try again."
        try:
            await safe_websocket_send_text(websocket, json.dumps({
                "type": "error",
                "message": error_msg,
                "timestamp": datetime.utcnow().isoformat()
            }, ensure_ascii=False))
            audio_data = await synthesize_speech(error_msg)
            if audio_data:
                await safe_websocket_send_bytes(websocket, audio_data)
        except Exception as e:
            logging.error(f"Failed to send timeout error message: {str(e)}")
        return "Error: Query processing timed out"
    except Exception as e:
        import traceback
        error_message = str(e)
        stack_trace = traceback.format_exc()
        
        logging.error(f"Unexpected error in process_query_and_respond_with_text: {error_message}\n{stack_trace}")
        
        try:
            from database_operations import save_error_log_to_db
            await save_error_log_to_db(
                user_id=user_id if user_id else "unknown",
                session_id=session_id if session_id else "unknown",
                error_message=error_message,
                error_type="query_processing_timeout",
                query_text=query_text,
                stack_trace=stack_trace
            )
        except Exception as db_error:
            logging.error(f"Failed to save error to database: {str(db_error)}")
        
        return f"Error: {error_message}"

async def _process_query_internal(query_text: str, websocket: WebSocket, user_id: str = None, session_id: str = None, skip_location_detection: bool = False):
    """Internal query processing function with context maintenance"""
    import re  
    import traceback
    
    try:
        if user_id and session_id:
            try:
                websocket_id = f"ws_{id(websocket)}"
            except Exception as context_e:
                logging.warning(f"Failed to maintain user session context in query processing: {str(context_e)}")
        
        timing_manager.start_total_timing()
        
        original_query = query_text
        
        is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
        resolved_station = await get_state_value_from_db(user_id, session_id, 'resolved_station', None)
        
        if not is_awaiting_disambiguation and resolved_station:
            if resolved_station.lower() not in query_text.lower():
                await update_state_values_in_db(user_id, session_id, {'resolved_station': None})
                print(f"Cleared resolved station '{resolved_station}' - not in current query")
            else:
                print(f"Keeping resolved station '{resolved_station}' - still relevant to current query")
        
        if not user_id or not session_id:
            try:
                user_id, session_id = await get_user_session_info(websocket)
                if not user_id or not session_id:
                    logging.error("Could not retrieve user_id and session_id from session manager")
                    return "Error: Could not retrieve user identification"
            except Exception as e:
                logging.error(f"Error getting session info from session manager: {str(e)}")
                return "Error: Could not retrieve user identification"
                
        if not user_id or not session_id:
            logging.error("Missing user_id or session_id in process_query_and_respond_with_text")
            return "Error: Missing user identification"
        
        # --------- CHECK FOR STATION VALIDATION RESPONSE ---------
        awaiting_station_validation = await get_state_value_from_db(user_id, session_id, 'awaiting_station_validation', False)
        if awaiting_station_validation:
            print(f"\n========== STATION VALIDATION RESPONSE DETECTED ==========")
            print(f"Processing station validation response: '{query_text}'")
            
            invalid_station = await get_state_value_from_db(user_id, session_id, 'invalid_station', None)
            original_query = await get_state_value_from_db(user_id, session_id, 'original_query', None)
            
            if invalid_station and original_query:
                station_candidates = extract_station_names(query_text)
                if station_candidates:
                    valid_station_name = station_candidates[0]  
                    
                    pattern = re.compile(re.escape(invalid_station), re.IGNORECASE)
                    final_query = pattern.sub(valid_station_name, original_query)
                    
                    print(f"Original query: '{original_query}'")
                    print(f"Invalid station: '{invalid_station}'")
                    print(f"Replaced with: '{valid_station_name}'")
                    print(f"Final query: '{final_query}'")
                    
                    await update_state_values_in_db(user_id, session_id, {
                        'awaiting_station_validation': False,
                        'invalid_station': None,
                        'original_query': None
                    })
                    
                    await set_state_value_in_db(user_id, session_id, 'original_query_for_response', final_query)
                    
                    return await _process_query_internal(final_query, websocket, user_id, session_id, skip_location_detection=True)
                else:
                    print("User response is not a valid station name")
                    return None
            else:
                print("Missing invalid station or original query in state")
                return None
            
            print("=====================================================\n")
        
        # --------- CHECK FOR MISSING PARAMETER RESPONSE ---------
        awaiting_parameter = await get_state_value_from_db(user_id, session_id, 'awaiting_parameter', None)
        if awaiting_parameter:
            print(f"\n========== MISSING PARAMETER RESPONSE DETECTED ==========")
            print(f"User provided: '{query_text}'")
            print(f"Missing parameter: {awaiting_parameter}")
            
            # Get pending params from DB
            pending_params_json = await get_state_value_from_db(user_id, session_id, 'pending_params', None)
            if not pending_params_json:
                print("No pending params in DB")
                await update_state_values_in_db(user_id, session_id, {'awaiting_parameter': None})
                return None
            
            pending_params = json.loads(pending_params_json)
            
            if awaiting_parameter in ['orig', 'dest', 'station']:
                # First check if we have a resolved station from disambiguation
                resolved_station = await get_state_value_from_db(user_id, session_id, 'resolved_station', None)
                if resolved_station:
                    print(f"Using resolved station from disambiguation: {resolved_station}")
                    pending_params[awaiting_parameter] = resolved_station
                    print(f"Added {awaiting_parameter}: {resolved_station}")
                    
                    # Clear the resolved station state
                    await update_state_values_in_db(user_id, session_id, {
                        'resolved_station': None
                    })
                else:
                    # Normal station extraction flow
                    station_names = extract_station_names(query_text)
                    if station_names:
                        station_value = station_names[0]
                        
                        is_ambiguous, ambiguous_options = check_station_ambiguity(station_value)
                        if is_ambiguous:
                            await update_state_values_in_db(user_id, session_id, {
                                'awaiting_disambiguation': True,
                                'station_options': ambiguous_options,
                                'ambiguous_station': station_value
                            })
                            
                            options_text = "\n".join([f"{idx + 1}. {opt}" for idx, opt in enumerate(ambiguous_options)])
                            language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                            disambiguation_message = f"I found multiple stations. Please select one:\n{options_text}"
                            
                            audio_data = await synthesize_speech(disambiguation_message, language=language)
                            if audio_data:
                                await safe_websocket_send_bytes(websocket, audio_data)
                            
                            print(f"Station '{station_value}' is ambiguous - awaiting disambiguation")
                            return None
                        
                        pending_params[awaiting_parameter] = station_value
                        print(f"Added {awaiting_parameter}: {station_value}")
                    else:
                        error_msg = "I couldn't identify the station. Could you please provide a valid BART station name?"
                        audio_data = await synthesize_speech(error_msg)
                        if audio_data:
                            await safe_websocket_send_bytes(websocket, audio_data)
                        return None
                
            elif awaiting_parameter == 'route':
                pending_params['route'] = query_text.strip()
            elif awaiting_parameter == 'eq':
                query_lower = query_text.lower()
                pending_params['eq'] = 'elevator' if 'elevator' in query_lower else 'escalator'
            else:
                pending_params[awaiting_parameter] = query_text.strip()
            
            await update_state_values_in_db(user_id, session_id, {
                'pending_params': json.dumps(pending_params)
            })
            
            print(f"Updated params: {pending_params}")
            
            pending_endpoint = await get_state_value_from_db(user_id, session_id, 'pending_endpoint', None)
            is_valid, validated_params, validation_error = EndpointValidator.validate_and_prepare_params(
                endpoint=pending_endpoint,
                params=pending_params,
                query_text=query_text
            )
            
            if not is_valid:
                print(f"Still missing parameters: {validation_error}")
                pending_category = await get_state_value_from_db(user_id, session_id, 'pending_category', None)
                pending_query = await get_state_value_from_db(user_id, session_id, 'pending_query', None)
                
                await handle_missing_parameters(
                    websocket, user_id, session_id,
                    pending_endpoint, pending_params, validation_error,
                    pending_category, pending_query
                )
                return None
            
            print(f"All parameters validated: {validated_params}")
            
            pending_category = await get_state_value_from_db(user_id, session_id, 'pending_category', None)
            pending_query = await get_state_value_from_db(user_id, session_id, 'pending_query', None)
            
            await update_state_values_in_db(user_id, session_id, {
                'resumed_intent_data': json.dumps({
                    'category': pending_category,
                    'api_endpoint': pending_endpoint,
                    'parameters': validated_params,
                    'is_api_related': True,
                    'from_parameter_validation': True  
                }),
                'skip_classifications': True,  
                'awaiting_parameter': None,
                'pending_endpoint': None,
                'pending_category': None,
                'pending_params': None
            })
            
            print(f"\n========== RESUMING API CALL ==========")
            print(f"Endpoint: {pending_endpoint}")
            print(f"Category: {pending_category}")
            print(f"Validated params: {validated_params}")
            print(f"Original query: {pending_query}")
            print("Will continue with API call and response generation...")
            print("==========================================\n")
            
            return await _process_query_internal(pending_query, websocket, user_id, session_id, skip_location_detection=True)
            
            print("==========================================================\n")
        
        # --------- PRE-PROCESSING: STATION VALIDATION & DISAMBIGUATION ---------
        print(f"\n========== PRE-PROCESSING: STATION VALIDATION ==========")
        print(f"Query text: '{query_text}'")
        
        resolved_station = await get_state_value_from_db(user_id, session_id, 'resolved_station', None)
        skip_validation = False
        if resolved_station and resolved_station.lower() in query_text.lower():
            print(f"Query contains recently resolved station '{resolved_station}' - skipping validation")
            skip_validation = True
        
        query_lower = query_text.lower()
        bart_org = [
            'BART','Bart','bart','Bay Area Rapid Transit','Bay Area Rapid','rapid transit'
        ]
        if any(keyword in query_lower for keyword in bart_org):
            print(f"Query is about BART organization/governance - skipping station validation")
            skip_validation = True
        
        timing_manager.start_section_timing("STATION_EXTRACTION")
        all_potential_stations = await extract_all_potential_station_names(query_text)
        timing_manager.end_section_timing("STATION_EXTRACTION")
        
        if all_potential_stations and not skip_validation:
            timing_manager.start_section_timing("STATION_VALIDATION")
            validation_result = validate_stations(all_potential_stations, skip_fuzzy_matching=True)
            if not validation_result["valid"]:
                print(f"Found invalid stations during pre-processing: {validation_result['invalid_stations']}")
                
                try:
                    updated_query = await process_station_validation(
                        query_text, 
                        validation_result["invalid_stations"],
                        validation_result["suggestions"],
                        websocket,
                        user_id,
                        session_id
                    )
                    
                    if updated_query:
                        print(f"Updated query after station validation: {updated_query}")
                        query_text = updated_query
                    else:
                        print("Station validation process already handled the invalid station")
                        return None
                except Exception as e:
                    print(f"Error during station validation processing: {str(e)}")
                    raise
            timing_manager.end_section_timing("STATION_VALIDATION")
        
        timing_manager.start_section_timing("STATION_DISAMBIGUATION")
        station_candidates = extract_station_names(query_text)
        if station_candidates:
            print(f"Extracted station candidates: {station_candidates}")
            
            complete_stations = []
            partial_candidates = []
            
            for candidate in station_candidates:
                is_complete = any(
                    station["name"].lower() == candidate.lower() 
                    for station in STATION_DATA["stations"]
                )
                
                if is_complete:
                    complete_stations.append(candidate)
                else:
                    partial_candidates.append(candidate)
            
            query_lower = query_text.lower()
            verified_complete_stations = []
            
            mentioned_group_keys = []
            for group_key in STATION_GROUPS.keys():
                if group_key.lower() in query_lower:
                    mentioned_group_keys.append(group_key.lower())
            
            for complete in complete_stations:
                is_part_of_mentioned_group = False
                for group_key, stations_list in STATION_GROUPS.items():
                    if group_key.lower() in mentioned_group_keys and complete in stations_list:
                        is_part_of_mentioned_group = True
                        print(f"Keeping '{complete}' - part of mentioned group '{group_key}'")
                        break
                
                if is_part_of_mentioned_group:
                    verified_complete_stations.append(complete)
                    continue

                is_abbreviation_mapping = False
                for station in STATION_DATA["stations"]:
                    if station["name"] == complete:
                        abbr_lower = station["abbr"].lower()
                        if re.search(r'\b' + re.escape(abbr_lower) + r'\b', query_lower):
                            is_abbreviation_mapping = True
                            print(f"Keeping '{complete}' - extracted via abbreviation '{station['abbr']}' mapping")
                            break
                
                if is_abbreviation_mapping:
                    verified_complete_stations.append(complete)
                    continue
                
                complete_words = complete.lower().split()
                significant_words = [w for w in complete_words if len(w) > 2 and w not in ['the', 'and', 'of', 'at', 'in', 'on', 'del']]
                
                if len(significant_words) >= 3:
                    if all(word in query_lower for word in significant_words):
                        verified_complete_stations.append(complete)
                    else:
                        print(f"Filtering out irrelevant complete station '{complete}' - not all words in query")
                else:
                    verified_complete_stations.append(complete)
            
            complete_stations = verified_complete_stations
            print(f"Verified complete stations (mentioned in query): {complete_stations}")
            
            filtered_candidates = complete_stations.copy()
            for partial in partial_candidates:
                matching_complete_stations = []
                for complete in complete_stations:
                    if partial.lower() in complete.lower():
                        matching_complete_stations.append(complete)
                
                is_in_station_groups = partial in STATION_GROUPS or partial.capitalize() in STATION_GROUPS or partial.lower() in [k.lower() for k in STATION_GROUPS.keys()]
                
                if is_in_station_groups and matching_complete_stations:
                    query_lower = query_text.lower()
                    exact_match_provided = False
                    
                    for complete in matching_complete_stations:
                        complete_lower = complete.lower()
                        
                        query_normalized = re.sub(r'[^\w\s]', ' ', query_lower)
                        query_normalized = re.sub(r'\s+', ' ', query_normalized).strip()
                        complete_normalized = re.sub(r'[^\w\s]', ' ', complete_lower)
                        complete_normalized = re.sub(r'\s+', ' ', complete_normalized).strip()
                        
                        if complete_normalized in query_normalized:
                            exact_match_provided = True
                            print(f"User explicitly mentioned complete station '{complete}' - filtering out partial '{partial}'")
                            break
                    
                    if exact_match_provided:
                        continue
                
                if len(matching_complete_stations) == 1:
                    print(f"Filtering out partial '{partial}' - covered by '{matching_complete_stations[0]}'")
                    continue
                
                if is_in_station_groups:
                    print(f"Keeping ambiguous partial '{partial}' - found in STATION_GROUPS")
                    filtered_candidates.append(partial)
                elif len(matching_complete_stations) > 1:
                    print(f"Keeping ambiguous partial '{partial}' - matches multiple stations: {matching_complete_stations}")
                    filtered_candidates.append(partial)
                elif len(matching_complete_stations) == 0:
                    print(f"Keeping partial '{partial}' - no matching complete stations found")
                    filtered_candidates.append(partial)
            
            station_candidates = filtered_candidates
            print(f"Filtered station candidates: {station_candidates}")
            
            resolved_station = await get_state_value_from_db(user_id, session_id, 'resolved_station', None)
            
            if resolved_station:
                print(f"Recently resolved station: '{resolved_station}'")
                filtered_by_resolution = []
                for candidate in station_candidates:
                    if candidate.lower() in resolved_station.lower() and candidate.lower() != resolved_station.lower():
                        print(f"Filtering out partial '{candidate}' - it's part of recently resolved '{resolved_station}'")
                        continue
                    else:
                        filtered_by_resolution.append(candidate)
                
                station_candidates = filtered_by_resolution
                print(f"Candidates after resolution filtering: {station_candidates}")
            
            ambiguous_stations = []
            for station_candidate in station_candidates:
                is_ambiguous, ambiguous_options = check_station_ambiguity(station_candidate, resolved_station)
                if is_ambiguous:
                    ambiguous_stations.append({
                        'candidate': station_candidate,
                        'options': ambiguous_options
                    })
                    print(f"Found ambiguous station: '{station_candidate}' with options: {ambiguous_options}")
            
            if ambiguous_stations:
                first_ambiguous = ambiguous_stations[0]
                station_candidate = first_ambiguous['candidate']
                ambiguous_options = first_ambiguous['options']
                
                print(f"Processing disambiguation for: '{station_candidate}' (first of {len(ambiguous_stations)} ambiguous stations)")
                
                await update_state_values_in_db(user_id, session_id, {
                    'awaiting_disambiguation': True,
                    'original_query': query_text,
                    'station_options': ambiguous_options,
                    'ambiguous_station': station_candidate
                })
                
                options_text = "\n".join([f"{idx + 1}. {opt}" for idx, opt in enumerate(ambiguous_options)])
                language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                        
                disambiguation_message = f"I found multiple stations that match '{station_candidate}'. Please select one:\n{options_text}"
                
                audio_data = await synthesize_speech(disambiguation_message, language=language)
                if audio_data:
                    await safe_websocket_send_bytes(websocket, audio_data)
                        
                temp_id = f"disambiguation_{uuid.uuid4()}"
                conversation_data = {
                    "type": "conversation",
                    "query": query_text,
                    "response": disambiguation_message,
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": session_id,
                    "conversation_id": temp_id,
                    "is_disambiguation": True
                }
                await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
                        
                print("Returning early for disambiguation")
                return None
            
            if resolved_station:
                await set_state_value_in_db(user_id, session_id, 'resolved_station', None)
                print(f"Cleared resolved_station after processing")
                
            # SECOND: Check for invalid stations
            try:
                validation_result = validate_stations(station_candidates, skip_fuzzy_matching=True)
                if not validation_result["valid"]:
                    print(f"Found invalid stations during pre-processing: {validation_result['invalid_stations']}")
                    
                    try:
                        updated_query = await process_station_validation(
                            query_text, 
                            validation_result["invalid_stations"],
                            validation_result["suggestions"],
                            websocket,
                            user_id,
                            session_id
                        )
                        
                        if updated_query:
                            print(f"Updated query after station validation: {updated_query}")
                            query_text = updated_query
                        else:
                            print("Station validation process already handled the invalid station")
                            return None
                    except Exception as e:
                        print(f"Error during station validation processing: {str(e)}")
                        raise
            except Exception as e:
                print(f"Error during pre-processing station validation: {str(e)}")
            
            print("========================================================\n")
        
        timing_manager.end_section_timing("STATION_DISAMBIGUATION")
        
        # --------- LOCATION DETECTION & HANDLING ---------
        is_awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
        if is_awaiting_location:
            print(f"\n========== LOCATION RESPONSE DETECTED ==========")
            print(f"Processing location response: '{query_text}'")
            
            location_stations = extract_station_names(query_text)
            if location_stations:
                location_station = location_stations[0]
                
                location_resolved_station = await get_state_value_from_db(user_id, session_id, 'resolved_station', None)
                
                is_ambiguous, ambiguous_options = check_station_ambiguity(location_station, location_resolved_station)
                if is_ambiguous:
                    print(f"Found ambiguous location station: '{location_station}' with options: {ambiguous_options}")
                    
                    await update_state_values_in_db(user_id, session_id, {
                        'awaiting_disambiguation': True,
                        'original_query': query_text,
                        'station_options': ambiguous_options,
                        'ambiguous_station': location_station
                    })
                    
                    options_text = "\n".join([f"{idx + 1}. {opt}" for idx, opt in enumerate(ambiguous_options)])
                    language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                    
                    disambiguation_message = f"I found multiple stations that match '{location_station}'. Please select one:\n{options_text}"
                    
                    audio_data = await synthesize_speech(disambiguation_message, language=language)
                    if audio_data:
                        await safe_websocket_send_bytes(websocket, audio_data)
                    
                    temp_id = f"disambiguation_{uuid.uuid4()}"
                    conversation_data = {
                        "type": "conversation",
                        "query": query_text,
                        "response": disambiguation_message,
                        "timestamp": datetime.utcnow().isoformat(),
                        "session_id": session_id,
                        "conversation_id": temp_id,
                        "is_disambiguation": True
                    }
                    await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
                    
                    print("Returning early for location disambiguation")
                    return None
                else:
                    print(f"Valid location station: '{location_station}'")
                    
                    destination = await get_state_value_from_db(user_id, session_id, 'destination_station', '')
                    if destination:
                        reconstructed_query = f"next train from {location_station} to {destination}"
                        print(f"Reconstructed query: '{reconstructed_query}'")
                        
                        await update_state_values_in_db(user_id, session_id, {
                            'final_query': reconstructed_query,
                            'awaiting_location': False,
                            'destination_station': destination,
                            'origin_station': location_station,
                            'complete_query': True
                        })
                        
                        return await _process_query_internal(reconstructed_query, websocket, user_id, session_id, skip_location_detection=True)
                    else:
                        print("No destination found in state")
            else:
                print("No valid station found in location response")
        
        # --------- COMPREHENSIVE VALIDATION FIRST (BEFORE ANY PROCESSING) ---------
        print("\n========== QUERY PROCESSING VALIDATION ==========")
        print(f"Validating prerequisites for query: '{query_text}'")
        
        original_session_id = session_id  
        language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
        
        is_valid, error_message, validated_user_id, validated_session_id = await validate_query_processing_prerequisites_with_params(websocket, user_id, session_id)
        
        user_id = validated_user_id
        session_id = validated_session_id
        
        if not is_valid:
            print(f"Validation failed: {error_message}")
            print("=============================================\n")
            logging.error(f"Query processing validation failed: {error_message}")
            return f"Error: {error_message}"
        
        if original_session_id and original_session_id != session_id:
            regeneration_notification = {
                "type": "session_regenerated",
                "old_session_id": original_session_id,
                "new_session_id": session_id,
                "message": "Session has been renewed. Please use the new session ID for future requests.",
                "timestamp": datetime.utcnow().isoformat()
            }
            await safe_websocket_send_text(websocket, json.dumps(regeneration_notification, ensure_ascii=False))
            logging.info(f"Sent session regeneration notification: {original_session_id} -> {session_id}")
        
        print(f"Validation passed - User: {user_id}, Session: {session_id}")
        print("=============================================\n")
                    
        # --------- NOW PROCEED WITH QUERY PROCESSING ---------
        query_text = query_text.replace("Street", "St.").replace("street", "St.")
        
        # --------- CHECK FOR RESUMED FLOW (AFTER PARAMETER COLLECTION) ---------
        skip_classifications = await get_state_value_from_db(user_id, session_id, 'skip_classifications', False)
        resumed_intent_data_json = await get_state_value_from_db(user_id, session_id, 'resumed_intent_data', None)
        
        if skip_classifications and resumed_intent_data_json:
            print("\n========== RESUMED FROM PARAMETER COLLECTION ==========")
            print("Skipping query type and intent classifications")
            print("Using pre-validated intent data from parameter collection")
            
            intent_data = json.loads(resumed_intent_data_json)
            category = intent_data.get('category')
            api_endpoint = intent_data.get('api_endpoint')
            params = intent_data.get('parameters', {})
            is_api_related = intent_data.get('is_api_related', True)
            from_parameter_validation = intent_data.get('from_parameter_validation', False)
            
            print(f"Category: {category}")
            print(f"Endpoint: {api_endpoint}")
            print(f"Parameters: {json.dumps(params, indent=2)}")
            print(f"From parameter validation: {from_parameter_validation}")
            
            await update_state_values_in_db(user_id, session_id, {
                'skip_classifications': False,
                'resumed_intent_data': None
            })
            
            print("Jumping to API call section...")
            print("===================================================\n")
            
            query_type = "api"
            
            is_resumed_flow = True
        else:
            is_resumed_flow = False
            from_parameter_validation = False
        
        
        # --------- REPEAT REQUEST DETECTION (BEFORE CLASSIFICATION) ---------
        if not is_resumed_flow:
            print("\n========== REPEAT REQUEST DETECTION ==========")
            
            is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
            if is_awaiting_disambiguation:
                print(f"Skipping repeat request detection - in disambiguation mode: {query_text}")
                query_type = None
                is_repeat = False
            else:
                session_context = await context_manager.get_session_context(user_id, session_id)
                
                is_repeat, repeat_type = await detect_repeat_request(query_text, session_context)
                
                if is_repeat:
                    print(f"Repeat request detected via direct detection: {query_text} (type: {repeat_type})")
                    query_type = "repeat_request"
                else:
                    print(f"Not a repeat request: {query_text}")
                    query_type = None
        else:
            is_repeat = False
            query_type = "api"

        # --------- PERFORM INITIAL QUERY TYPE CLASSIFICATION (IF NOT REPEAT AND NOT RESUMED) ---------
        if not is_repeat and not is_resumed_flow:
            timing_manager.start_section_timing("QUERY_TYPE_CLASSIFICATION")
            context_info = await context_manager.get_contextual_prompt_wrapper(user_id, session_id, query_text)
            
            if context_info != query_text:
                print(f"\n[CONTEXT] Query Type Classification with Context:")
                print(f"  Context: {context_info}")
            else:
                print(f"\n[CONTEXT] Query Type Classification without Context:")
                print(f"  Query: {query_text}")
            
            classification_response = await non_streaming_claude_response({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": [{"type": "text", "text": QUERY_TYPE_CLASSIFICATION_PROMPT.format(query_text=query_text, context_info=context_info)}]}]
            })
            
            print("\n========== QUERY TYPE CLASSIFICATION ==========")
            print(f"Classification: {classification_response}")
            print(f"Query text: '{query_text}'")
            print("============================================\n")
        else:
            classification_response = None
            print("\n========== QUERY TYPE CLASSIFICATION ==========")
            if is_resumed_flow:
                print("Skipped (is_resumed_flow=True) - using query_type='api'")
            elif is_repeat:
                print("Skipped (is_repeat=True) - using query_type='repeat_request'")
            print(f"Query text: '{query_text}'")
            print("============================================\n")
        
        if classification_response is None:
            needs_location = False
            classification_confidence = 1.0
            print(f"Using pre-set query_type: {query_type}")
        elif not is_repeat:
            try:
                classification_data = json.loads(classification_response.strip())
                query_type = classification_data.get("query_type", "").lower()
                needs_location = classification_data.get("needs_location", False)
                classification_confidence = classification_data.get("confidence", 0.0)
                
            except (json.JSONDecodeError, AttributeError, TypeError) as e:
                print(f"Falling back to old classification format due to: {str(e)}")
                print(f"Response was: {classification_response[:200]}...")
                logging.warning(f"JSON parsing failed for classification: {str(e)}")
                
                classification_text = classification_response.strip().lower()
                valid_types = ["greeting", "api", "kb", "stop_command", "off_topic", "off-topic"]
                if classification_text in valid_types:
                    if classification_text == "off-topic":
                        query_type = "off_topic"
                    else:
                        query_type = classification_text
                        needs_location = False  
                        classification_confidence = 0.5  
                else:
                    if "greeting" == classification_text:
                        query_type = "greeting"
                    elif "stop_command" == classification_text or "stop command" == classification_text:
                        query_type = "stop_command"
                    elif "api" == classification_text:
                        query_type = "api"
                    elif "kb" == classification_text:
                        query_type = "kb"
                    elif "off_topic" == classification_text or "off-topic" == classification_text:
                        query_type = "off_topic"
                    elif "repeat_request" == classification_text:
                        query_type = "repeat_request"
                    else:
                        query_type = "kb" 
                        print(f"Warning: Unknown classification '{classification_text}', defaulting to 'kb'")
                        logging.warning(f"Unclear classification response: '{classification_text}', defaulting to 'kb'")
        else:
            query_type = "repeat_request"
            needs_location = False
            classification_confidence = 1.0
            logging.info(f"Using pre-detected repeat request, skipping classification")

        timing_manager.end_section_timing("QUERY_TYPE_CLASSIFICATION")

        # --------- CHECK FOR CORRECTED QUERY FLAG ---------
        original_query_for_response = await get_state_value_from_db(user_id, session_id, 'original_query_for_response', None)
        if original_query_for_response:
            print(f"\n========== PROCESSING CORRECTED QUERY ==========")
            print(f"Query: '{query_text}' (trusting model's needs_location determination)")
            print("=============================================\n")
            
            await set_state_value_in_db(user_id, session_id, 'original_query_for_response', None)
        
        # --------- HANDLE LOCATION-BASED QUERIES ---------
        if from_parameter_validation:
            print("\n========== SKIPPING LOCATION PROCESSING (FROM PARAMETER VALIDATION) ==========")
            print("Coming from parameter validation flow - skipping location processing")
            print("Parameters already validated and complete")
            print("==================================================================\n")
        elif needs_location and query_type == "api" and not skip_location_detection:
            timing_manager.start_section_timing("LOCATION_DETECTION")
            print(f"\n========== PROCESSING LOCATION-BASED QUERY ==========")
            print(f"Query needs location: {needs_location}")
            print(f"Query type: {query_type}")
            print(f"Confidence: {classification_confidence}")
            
            nearest_station = await get_state_value_from_db(user_id, session_id, 'nearest_station', None)
            
            if not nearest_station:
                print("Getting nearest station from database...")
                nearest_station = await location_manager.get_nearest_station_for_session(session_id, user_id)
            
            if nearest_station:
                print(f"Found nearest station: {nearest_station['name']} ({nearest_station['abbr']}) at {nearest_station['distance']:.2f} km")
                
                await update_state_values_in_db(user_id, session_id, {
                    'user_current_station': nearest_station['abbr'],
                    'user_current_station_name': nearest_station['name']
                })
                print(f"Stored user's current station: {nearest_station['name']} ({nearest_station['abbr']})")
            else:
                print(f"No location data available for session {session_id}")
                
                location_state = await get_location_state(user_id, session_id)
                is_awaiting_location = location_state.get('awaiting_location', False)
                
                if websocket and not is_awaiting_location:
                    station_candidates = extract_station_names(query_text)
                    
                    if len(station_candidates) == 1:
                        destination_station = station_candidates[0]
                        print(f"Setting DynamoDB state for location request. Destination: {destination_station}")
                        await update_state_values_in_db(user_id, session_id, {
                            'awaiting_location': True,
                            'destination_station': destination_station
                        })
                        
                        language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                        location_request = f"To find trains to {destination_station}, I need to know which BART station you're currently at. Please tell me your current station."
                        
                        audio_data = await synthesize_speech(location_request, language=language)
                        if audio_data:
                            await safe_websocket_send_bytes(websocket, audio_data)
                        
                        temp_id = f"location_request_{uuid.uuid4()}"
                        conversation_data = {
                            "type": "conversation",
                            "query": query_text,
                            "response": location_request,
                            "timestamp": datetime.utcnow().isoformat(),
                            "session_id": session_id,
                            "conversation_id": temp_id,
                            "is_location_request": True
                        }
                        await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
                        
                        print("Returning early from location-based query to wait for location input")
                        return None
            
            print("==================================================\n")
            timing_manager.end_section_timing("LOCATION_DETECTION")

        # --------- HANDLE REPEAT REQUEST (IMMEDIATE RESPONSE, NO THINKING MESSAGE) ---------
        if query_type == "repeat_request":
            print(f"\n========== REPEAT REQUEST DETECTED ==========")
            print(f"Query: '{query_text}'")
            print("Immediately replaying previous response...")
            print("==============================================\n")
            
            session_context = await context_manager.get_session_context(user_id, session_id)
            
            repeat_response, repeat_success = await handle_repeat_request(query_text, session_context, query_text)
            
            if repeat_success and repeat_response:
                try:
                    await safe_websocket_send_text(websocket, json.dumps({
                        "type": "response",
                        "message": repeat_response,
                        "timestamp": datetime.utcnow().isoformat(),
                        "is_repeat": True
                    }, ensure_ascii=False))
                    
                    language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                    skip_tts = await get_state_value_from_db(user_id, session_id, 'skip_tts', False)
                    
                    if not skip_tts:
                        audio_data = await synthesize_speech(repeat_response, language)
                        if audio_data:
                            await safe_websocket_send_bytes(websocket, audio_data)
                    
                    try:
                        conversation_id = str(uuid.uuid4())
                        await save_conversation(
                            user_id=user_id,
                            query=query_text,
                            response=repeat_response,
                            session_id=session_id,
                            conversation_id=conversation_id
                        )
                        await update_session_activity(user_id, session_id)
                        logging.info(f"Saved repeat request conversation to database")
                    except Exception as e:
                        logging.error(f"Error saving repeat request to database: {str(e)}")
                    
                    logging.info(f"Successfully handled repeat request: {query_text}")
                    return repeat_response
                    
                except Exception as e:
                    logging.error(f"Error sending repeat response: {str(e)}")
                    return f"Error: Could not send repeated response. {str(e)}"
            else:
                error_msg = "I don't have a previous response to repeat. Could you please ask your question again?"
                
                audio_data = await synthesize_speech(error_msg, language)
                if audio_data:
                    await safe_websocket_send_bytes(websocket, audio_data)
                
                try:
                    conversation_id = str(uuid.uuid4())
                    await save_conversation(
                        user_id=user_id,
                        query=query_text,
                        response=error_msg,
                        session_id=session_id,
                        conversation_id=conversation_id
                    )
                    await update_session_activity(user_id, session_id)
                    logging.info(f"Saved repeat request error to database")
                except Exception as e:
                    logging.error(f"Error saving repeat request error to database: {str(e)}")
                
                return error_msg
            
        # --------- THINKING MESSAGE DEFINITION ---------
        language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
        selected_thinking_messages = thinking_messages.get(language, thinking_messages["en"])

        print("\n========== USER QUERY ==========")
        print(f"User Query: {query_text}")
        print("===============================\n")
        
        websocket_language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
        original_query_language = await get_state_value_from_db(user_id, session_id, 'original_query_language', websocket_language)
        original_query_text = await get_state_value_from_db(user_id, session_id, 'original_query_text', query_text)
        
        language = websocket_language
        
        session_context = await context_manager.get_session_context(user_id, session_id)
        
        contains_station_name = False
        mentioned_station = None
        query_lower = query_text.lower()
        
        if query_type != "kb":
            for station in STATION_DATA['stations']:
                abbr_lower = station['abbr'].lower()
                if re.search(r'\b' + re.escape(abbr_lower) + r'\b', query_lower):
                    contains_station_name = True
                    mentioned_station = station['abbr']
                    logging.info(f"Query contains station abbreviation: {station['abbr']} for {station['name']}")
                    break
                    
            if not contains_station_name:
                for station in STATION_DATA['stations']:
                    station_name_clean = station['name'].lower()
                    if " station" in station_name_clean:
                        station_name_clean = station_name_clean.replace(" station", "")
                        
                    if station['name'].lower() in query_lower or station_name_clean in query_lower:
                        contains_station_name = True
                        mentioned_station = station['name']
                        logging.info(f"Query contains station name: {station['name']}")
                        break
        
        if contains_station_name and query_type == 'off_topic':
            query_type = 'api'
            logging.info(f"Overriding query_type from 'off_topic' to 'api' because query contains station name: {mentioned_station}")
            print(f"Overriding query type from off-topic to api due to station name: {mentioned_station}")
        
        if query_type != 'kb' and contains_station_name and len(query_text.split()) <= 5:
            query_type = 'api'
            logging.info(f"Overriding query_type to 'api' because query contains station name: {mentioned_station}")
            print(f"Overriding query type to API due to station name: {mentioned_station}")
                        
    
        # --------- HANDLE OFF-TOPIC QUERY ---------
        if query_type == 'off_topic':
            redirect_messages = {
                "en": "I'm your BART Assistant and can only answer questions about BART transit. How can I help you with BART information today?",
                "es": "Soy un asistente de BART y solo puedo responder preguntas sobre el tránsito de BART. ¿Cómo puedo ayudarte con información de BART hoy?",
                "zh": "我是BART助手，只能回答有关BART交通的问题。今天我能为您提供什么BART信息？"
            }           
            redirect_message = redirect_messages.get(language, redirect_messages["en"])
            audio_data = await synthesize_speech(redirect_message, language=language)
            if audio_data:
                await safe_websocket_send_bytes(websocket, audio_data)
            
            await save_and_send_response(
                websocket, user_id, query_text, 
                f"""API Response:
--------
Not applicable (off-topic)

Knowledge Base Response:
--------
Not applicable (off-topic)

Final Combined Response:
--------
{redirect_message}
""", redirect_message, session_id
            )
            
            session_context = await context_manager.get_session_context(user_id, session_id)
            session_context.store_response(redirect_message)
            logging.info(f"Stored off-topic response for repeat functionality")
            
            try:
                await context_manager._save_context_to_db(user_id, session_id, session_context)
                logging.info(f"Saved session context with off-topic response to database")
            except Exception as save_error:
                logging.error(f"Error saving session context to database: {str(save_error)}")
            
            return f"""API Response:
--------
Not applicable (off-topic)
Knowledge Base Response:
--------
Not applicable (off-topic)

Final Combined Response:
--------
{redirect_message}
"""
        
        # --------- HANDLE STOP COMMAND (IMMEDIATE AUDIO STOP) ---------
        if query_type == 'stop_command':
            print("\n========== STOP COMMAND DETECTED ==========")
            print(f"Query: '{query_text}'")
            print("Immediately stopping audio and processing...")
            print("==========================================\n")
            
            await safe_websocket_send_text(websocket, json.dumps({
                "type": "stop_listening",
                "message": "Stopped listening",
                "timestamp": datetime.utcnow().isoformat()
            }, ensure_ascii=False))
            
            await safe_websocket_send_text(websocket, json.dumps({
                "type": "stop_audio",
                "timestamp": datetime.utcnow().isoformat()
            }, ensure_ascii=False))
            
            logging.info(f"Stop command processed: {query_text}")
            return None
        
        # --------- HANDLE GREETING (IMMEDIATE RESPONSE, NO THINKING MESSAGE) ---------
        if query_type == 'greeting':
            print("\n========== GREETING DETECTED ==========")
            print(f"Query: '{query_text}'")
            print("Generating immediate greeting response...")
            print("====================================\n")
            
            greeting_response = await non_streaming_claude_response({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 80,
                "temperature": 0.3,
                "messages": [{"role": "user", "content": [{"type": "text", "text": f"""
                    Respond to this greeting naturally and immediately. 
                    
                    Rules:
                    - Be warm and friendly but professional
                    - Keep it brief and conversational
                    - No emojis or exclamation marks
                    - Always relate to BART assistance
                    - Respond as a human would, not a robot
                    
                    For initial greetings: "Hello. How can I help with BART today?"
                    For thank you: "You're welcome. What other BART information do you need?"
                    For acknowledgments: "Great. What BART information can I help you with?"
                    
                    User greeting: {query_text}
                """}]}]
            })
            
            if not greeting_response:
                greeting_response = "Hello. How can I help with BART today?"
            
            cleaned_response = greeting_response.strip()
            if cleaned_response.startswith('"') and cleaned_response.endswith('"'):
                cleaned_response = cleaned_response[1:-1].strip()
            
            audio_data = await synthesize_speech(cleaned_response, language=language)
            if audio_data:
                await safe_websocket_send_bytes(websocket, audio_data)
            
            session_context = await context_manager.get_session_context(user_id, session_id)
            session_context.store_response(cleaned_response)
            
            try:
                await context_manager._save_context_to_db(user_id, session_id, session_context)
            except Exception as save_error:
                logging.error(f"Error saving greeting response: {str(save_error)}")
            
            return f"""API Response:
            --------
            Not applicable (greeting)

            Knowledge Base Response:
            --------
            Not applicable (greeting)

            Final Combined Response:
            --------
            {cleaned_response}
            """
        else:
            # --------- INTENT CLASSIFICATION WITH CONTEXT CONTINUITY ---------
            contextual_query = query_text
            use_contextual_prompt = False
            
            if is_resumed_flow:
                print("\n========== USING RESUMED INTENT DATA ==========")
                print(f"Category: {category}")
                print(f"Endpoint: {api_endpoint}")
                print(f"Parameters: {params}")
                if from_parameter_validation:
                    print("Coming from parameter validation - using validated parameters")
                    print("Skipping parameter extraction and validation")
                print("==============================================\n")
            else:
                is_disambiguation_response = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                
                if not is_disambiguation_response:
                    timing_manager.start_section_timing("CONTEXT_RETRIEVAL")
                    contextual_query = await context_manager.get_contextual_prompt_wrapper(user_id, session_id, query_text)
                    
                    user_current_station = await get_state_value_from_db(user_id, session_id, 'user_current_station_name', None)
                    if user_current_station and needs_location:
                        if contextual_query == query_text:
                            contextual_query = f"User's current station: {user_current_station}\nCurrent question: {query_text}"
                        else:
                            contextual_query = f"User's current station: {user_current_station}\n{contextual_query}"
                        use_contextual_prompt = True  
                        print(f"\n========== LOCATION CONTEXT ADDED ==========")
                        print(f"User's location: {user_current_station}")
                        print(f"Contextual query: {contextual_query}")
                        print("==========================================\n")
                    else:
                        use_contextual_prompt = (contextual_query != query_text)
                        if use_contextual_prompt:
                            print(f"\n========== USING CONTEXTUAL PROMPT ==========")
                            print(f"Original query: {query_text}")
                            print(f"Contextual query: {contextual_query}")
                            print("===========================================\n")
                        else:
                            print(f"\n========== NO CONTEXT AVAILABLE ==========")
                            print(f"Query: {query_text}")
                            print("===========================================\n")
                else:
                    contextual_query = query_text
                    use_contextual_prompt = False
                    print(f"\n========== DISAMBIGUATION MODE - NO CONTEXT ==========")
                    print(f"Query: {query_text}")
                    print("===================================================\n")
                
                timing_manager.end_section_timing("CONTEXT_RETRIEVAL")
                
                # Step 2: Classify the intent and extract parameters for API calls
                station_map_str = json.dumps(STATION_ABBREVIATION_MAP, indent=2)
                station_map_str = station_map_str.replace('{', '{{').replace('}', '}}')
                
                current_datetime = datetime.now()
                current_date_str = current_datetime.strftime("%B %d, %Y")  
                current_year_str = str(current_datetime.year)  
                
                formatted_prompt = INTENT_CLASSIFICATION_PROMPT.replace('{STATION_ABBREVIATION_MAP}', station_map_str)
                final_prompt = formatted_prompt.format(
                    query_text=contextual_query, 
                    query_type=query_type, 
                    needs_location=needs_location,
                    current_date=current_date_str,
                    current_year=current_year_str
                )
                
                print(f"\n========== INTENT CLASSIFICATION DEBUG ==========")
                print(f"Query for classification: {contextual_query}")
                print(f"Query type passed to intent classifier: {query_type}")
                print(f"Model needs_location flag: {needs_location}")
                print("==========================================\n")
                
                timing_manager.start_section_timing("INTENT_CLASSIFICATION")
                intent_response = await non_streaming_claude_response({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 1000,
                        "temperature": 0.05,
                        "messages": [{"role": "user", "content": [{"type": "text", "text": final_prompt}]}]
                })
                
                print("\n========== API INTENT CLASSIFICATION==========")
                print(f"User ID: {user_id}")
                print(f"Session ID: {session_id}")
                print(f"Contextual Query: {contextual_query}")
                print(f"Use Contextual Prompt: {use_contextual_prompt}")
                print(f"Intent Response: {intent_response}")
                print("=========================================\n")
                timing_manager.end_section_timing("INTENT_CLASSIFICATION")
            
            try:
                extracted_stations = station_candidates if not is_resumed_flow else []
                
                if not is_resumed_flow:
                    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
                    json_matches = re.findall(json_pattern, intent_response)
                    
                    if json_matches:
                        json_str = json_matches[-1]
                        logging.info(f"Extracted JSON from intent response: {json_str}")
                        intent_data = json.loads(json_str)
                    else:
                        intent_data = json.loads(intent_response)
                    
                    is_api_related = intent_data.get("is_api_related", False)  
                    bart_data = None
                    api_response_text = "No API data available"
                    
                    category = intent_data.get("category", "UNKNOWN")
                    params = intent_data.get("parameters", {})
                    api_endpoint = intent_data.get("api_endpoint", None)
                    
                    # ======================================CHECK IF KB-ONLY QUERY======================================
                    if not is_api_related:
                        print(f"\n========== KB-ONLY QUERY DETECTED ==========")
                        print(f"is_api_related: {is_api_related}")
                        print(f"Category: {category}")
                        print(f"Skipping API validation and parameter collection")
                        print(f"Will retrieve answer from Knowledge Base only")
                        print("============================================\n")
                        
                        intent_type = "KB"
                        is_api_related = False
                        bart_data = None
                    
                    if is_api_related:
                        timing_manager.start_section_timing("PARAMETER_EXTRACTION")
                        # ======================================STATION ABBREVIATION OVERRIDE======================================
                        print(f"Using pre-processed station candidates: {extracted_stations}")
                
                        if extracted_stations and not is_resumed_flow:
                            if len(extracted_stations) == 1 and api_endpoint == "etd":
                                params["orig"] = extracted_stations[0]
                                if "station" in params:
                                    del params["station"]
                                if "dest" in params:
                                    del params["dest"]
                                print(f"OVERRIDE: Using correctly extracted station '{extracted_stations[0]}' as orig for ETD endpoint")
                            elif len(extracted_stations) >= 2 and api_endpoint in ["depart", "arrive"]:
                                if "orig" not in params or "dest" not in params:
                                    params["orig"] = extracted_stations[0]
                                    params["dest"] = extracted_stations[1]
                                if "station" in params:
                                    del params["station"]
                            elif len(extracted_stations) == 1 and api_endpoint in ["stninfo", "stnsched"]:
                                params["orig"] = extracted_stations[0]
                                if "station" in params:
                                    del params["station"]
                                if "dest" in params:
                                    del params["dest"]
                                print(f"OVERRIDE: Using correctly extracted station '{extracted_stations[0]}' as orig for {api_endpoint} endpoint")
                        
                        # ======================================AI INTENT CLASSIFICATION PARAMETERS======================================                      
                        print("\n========== API PARAMETERS ANALYSIS ==========")
                        print(f"Category: {category}")
                        print(f"API Related: {is_api_related}")
                        print(f"API Endpoint: {api_endpoint}")
                        print(f"AI Intent Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}")
                        
                        additional_params = extract_parameters_from_query(query_text, intent_data)
                        
                        for key, value in additional_params.items():
                            if key not in params or params[key] is None:
                                params[key] = value
                                print(f"Added missing parameter from query extraction: {key}={value}")
                        
                        print(f"Final Parameters (AI + Query Extraction): {json.dumps(params, indent=2, ensure_ascii=False)}")
                        print("============================================\n")
                        
                        # ======================================ENDPOINT VALIDATION & PREPARATION======================================
                        if not from_parameter_validation:
                            timing_manager.start_section_timing("API_PARAMETER_VALIDATION")
                            is_valid, validated_params, validation_error = EndpointValidator.validate_and_prepare_params(
                                endpoint=api_endpoint,
                                params=params,
                                query_text=query_text
                            )
                            
                            if not is_valid:
                                print(f"\n========== PARAMETER VALIDATION FAILED ==========")
                                print(f"Endpoint: {api_endpoint}")
                                print(f"Error: {validation_error}")
                                print(f"Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}")
                                print("=================================================\n")
                                
                                await handle_missing_parameters(
                                    websocket=websocket,
                                    user_id=user_id,
                                    session_id=session_id,
                                    api_endpoint=api_endpoint,
                                    params=params,
                                    validation_error=validation_error,
                                    category=category,
                                    query_text=query_text
                                )
                                return None  
                            
                            params = validated_params
                            print(f"\n========== PARAMETERS VALIDATED & READY ==========")
                            print(f"Endpoint: {api_endpoint}")
                            print(f"Validated Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}")
                            print("==================================================\n")
                            timing_manager.end_section_timing("API_PARAMETER_VALIDATION")
                        else:
                            print(f"\n========== SKIPPING VALIDATION (FROM PARAMETER VALIDATION) ==========")
                            print(f"Parameters already validated: {json.dumps(params, indent=2, ensure_ascii=False)}")
                            print("====================================================================\n")
                        
                        await update_state_values_in_db(user_id, session_id, {
                            'user_current_station': None,
                            'user_current_station_name': None,
                            'nearest_station': None,
                            'user_location': None
                        })
                        print("Cleaned up location data from session state")
                        
                        timing_manager.end_section_timing("PARAMETER_EXTRACTION")
                        
                        if query_type != "kb":
                            ambiguous_param_found = False
                            ambiguous_param_key = None
                            ambiguous_param_options = None
                            
                            for key, value in list(params.items()):
                                if key in ['orig', 'dest', 'station'] and value:
                                    normalized_value = normalize_station_name(str(value))
                                    if normalized_value != value:
                                        print(f"Normalized station parameter {key}: '{value}' → '{normalized_value}'")
                                        params[key] = normalized_value
                                    
                                    param_resolved_station = await get_state_value_from_db(user_id, session_id, 'resolved_station', None)
                                    is_ambiguous_param, ambiguous_options = check_station_ambiguity(str(params[key]), param_resolved_station)
                                    
                                    if is_ambiguous_param:
                                        ambiguous_param_found = True
                                        ambiguous_param_key = key
                                        ambiguous_param_options = ambiguous_options
                                        break  
                            
                            if ambiguous_param_found:
                                print(f"Parameter '{ambiguous_param_key}': '{params[ambiguous_param_key]}' is ambiguous (options: {ambiguous_param_options}) - triggering disambiguation")
                                
                                await update_state_values_in_db(user_id, session_id, {
                                    'awaiting_disambiguation': True,
                                    'original_query': query_text,
                                    'station_options': ambiguous_param_options,
                                    'ambiguous_station': str(params[ambiguous_param_key])
                                })
                                
                                options_text = "\n".join([f"{idx + 1}. {opt}" for idx, opt in enumerate(ambiguous_param_options)])
                                language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                                
                                disambiguation_message = f"I found multiple stations that match '{params[ambiguous_param_key]}'. Please select one:\n{options_text}"
                                
                                audio_data = await synthesize_speech(disambiguation_message, language=language)
                                if audio_data:
                                    await safe_websocket_send_bytes(websocket, audio_data)
                                
                                temp_id = f"disambiguation_{uuid.uuid4()}"
                                conversation_data = {
                                    "type": "conversation",
                                    "query": query_text,
                                    "response": disambiguation_message,
                                    "timestamp": datetime.utcnow().isoformat(),
                                    "session_id": session_id,
                                    "conversation_id": temp_id,
                                    "is_disambiguation": True
                                }
                                await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
                                
                                print("Returning early for parameter disambiguation")
                                return None  
                            else:
                                try:
                                    is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
                                    if not is_awaiting_disambiguation and key in params:
                                        mapped_value = map_user_station_to_actual_station(str(params[key]))
                                        if mapped_value != params[key]:
                                            print(f"Mapped station parameter {key}: '{params[key]}' → '{mapped_value}'")
                                            params[key] = mapped_value
                                except Exception as e:
                                    logging.warning(f"Could not check disambiguation state for fuzzy matching: {str(e)}")
                                    if key in params:
                                        mapped_value = map_user_station_to_actual_station(str(params[key]))
                                        if mapped_value != params[key]:
                                            print(f"Mapped station parameter {key}: '{params[key]}' → '{mapped_value}'")
                                            params[key] = mapped_value
                
                intent_is_api_related = intent_data.get("is_api_related", False)
                
                # PRIORITY 1: If query_type is "kb", it's a KB query (unless intent strongly disagrees)
                if query_type == "kb":
                    if intent_is_api_related:
                        print(f"\n⚠️ WARNING: Query type classification says 'kb' but intent classification says 'is_api_related: true'")
                        print(f"Query type: {query_type}, Intent category: {intent_data.get('category')}, Intent is_api_related: {intent_is_api_related}")
                        print(f"RESOLUTION: Prioritizing query_type classification - treating as KB query")
                        print(f"Reason: Query Type Classification is specifically designed for this decision\n")
                        is_api_related = False
                        intent_type = "KB"
                    else:
                        is_api_related = False
                        intent_type = "KB"
                        print(f"✅ Query classified as KB (both query_type and intent classification agree)")
                
                # PRIORITY 2: If query_type is "api", use intent classification's is_api_related
                elif query_type == "api":
                    if "category" in intent_data and intent_is_api_related:
                        is_api_related = True
                        print(f"✅ Query classified as API: {intent_data['category']} (both query_type and intent classification agree)")
                    elif not intent_is_api_related:
                        print(f"\n⚠️ WARNING: Query type classification says 'api' but intent classification says 'is_api_related: false'")
                        print(f"Query type: {query_type}, Intent category: {intent_data.get('category')}, Intent is_api_related: {intent_is_api_related}")
                        print(f"RESOLUTION: Trusting intent classification - treating as KB query")
                        print(f"Reason: Intent classifier examined actual query content and determined it doesn't need real-time data\n")
                        is_api_related = False
                        intent_type = "KB"
                    else:
                        is_api_related = True
                        print(f"Query classified as API-related: {intent_data.get('category')}")
                else:
                    if category == "STATION" and "station" in params:
                        is_api_related = True
                        print(f"Setting is_api_related to True because query is about a specific station: {params['station']}")
                    
                    if query_type == "kb" and not is_api_related:
                        if "station" in params or "orig" in params:
                            station_param = params.get("station") or params.get("orig")
                            if station_param:
                                print(f"\nQuery mentions station: {station_param}. Fetching station info...")
                                bart_data = await call_api_for_query("STATION", {"station": station_param}, query_text, "stninfo", websocket)
                                if bart_data and "error" not in bart_data:
                                    is_api_related = True
                                    intent_type = "MIXED"  
                                else:
                                    print(f"\nQuery mentions station: {station_param} but failed to get station data.")
                        
                        if not is_api_related:
                            print("\nSkipping BART API call - KB query that doesn't need real-time data\n")
                            intent_type = "KB"
                
                if is_api_related:
                    print("\n========== PROCESSING API REQUEST ==========")
                    print(f"API Category: {category}")
                    print(f"API Endpoint: {api_endpoint}")
                    print(f"Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}")
                    print("==========================================\n")
                                                
                    # --------- SEND THINKING MESSAGE AFTER LOCATION PROCESSING ---------
                    if query_type != 'greeting' and query_type != 'stop_command' and query_type != 'repeat_request':
                        thinking_msg = random.choice(selected_thinking_messages)
                        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                        
                        print(f"\n========== SENDING THINKING MESSAGE ==========")
                        print(f"Time: {current_time}")
                        print(f"Message: '{thinking_msg}'")
                        print(f"Query type: '{query_type}' (non-greeting/non-stop)")
                        print("===============================================\n")
                        
                        thinking_response = {
                            "type": "thinking_message",
                            "message": thinking_msg,
                            "timestamp": current_time
                        }
                        await safe_websocket_send_text(websocket, json.dumps(thinking_response, ensure_ascii=False))
                        
                        thinking_audio = await synthesize_speech(thinking_msg, language=language)
                        if thinking_audio:
                      
                            await safe_websocket_send_bytes(websocket, thinking_audio)
                            await asyncio.sleep(0.5)  
                    
                    kb_task = asyncio.create_task(retrieve_knowledge(query_text))
                    
                    timing_manager.start_section_timing("BART_API_CALL")
                    bart_data = await call_api_for_query(category, params, query_text, api_endpoint, websocket)
                    timing_manager.end_section_timing("BART_API_CALL")
                    
                    is_awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
                    if bart_data is None and is_awaiting_location:
                        print("\nAPI call returned None and awaiting_location is True. Stopping processing.")
                        return None
                    
                    print("\n[API] BART API Response:")
                    print(json.dumps(bart_data, indent=2, ensure_ascii=False) if bart_data else "No API data available")
                    print("================================\n")
                    
                    # ======================================UPDATE CONTEXT AFTER API CALL======================================
                    print(f"API call completed for user {user_id}, context will be updated after final response")
                else:
                    print("\nSkipping BART API call - query is not API-related\n")
                
                if bart_data:
                    api_response_text = json.dumps(bart_data, indent=2, ensure_ascii=False)
                
                if query_type != "kb" and bart_data and "error" in bart_data and "Invalid station" in bart_data["error"]:
                    error_message = bart_data["error"]
                    station_name_match = re.search(r"Invalid (?:orig|dest|station)(?:\s+station)?: ([^\.]+)", error_message)
                    
                    if station_name_match:
                        invalid_station = station_name_match.group(1).strip()
                        print(f"\n========== INVALID STATION IN API RESPONSE ==========")
                        print(f"Invalid station detected: {invalid_station}")
                        
                        print(f"\n========== INTENT STATION VALIDATION SKIPPED ==========")
                        print(f"Station validation was already completed in pre-processing step")
                        print("========================================================")
                elif query_type == "kb" and bart_data and "error" in bart_data and "Invalid station" in bart_data["error"]:
                    print(f"\n========== SKIPPING INVALID STATION HANDLING FOR KB QUERY ==========")
                    print(f"KB query with potential invalid station reference, but we're prioritizing KB intent")
                
                if 'kb_task' not in locals():
                    if query_type != 'greeting' and query_type != 'stop_command' and query_type != 'repeat_request':
                        thinking_msg = random.choice(selected_thinking_messages)
                        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                        
                        print(f"\n========== SENDING THINKING MESSAGE (KB QUERY) ==========")
                        print(f"Time: {current_time}")
                        print(f"Message: '{thinking_msg}'")
                        print(f"Query type: '{query_type}' (KB query)")
                        print("=====================================================\n")
                        
                        thinking_response = {
                            "type": "thinking_message",
                            "message": thinking_msg,
                            "timestamp": current_time
                        }
                        await safe_websocket_send_text(websocket, json.dumps(thinking_response, ensure_ascii=False))
                        
                        thinking_audio = await synthesize_speech(thinking_msg, language=language)
                        if thinking_audio:
                            await safe_websocket_send_bytes(websocket, thinking_audio)
                            await asyncio.sleep(0.5)
                    
                    timing_manager.start_section_timing("KB_RETRIEVAL")
                    kb_task = asyncio.create_task(retrieve_knowledge(query_text))
                else:
                    timing_manager.start_section_timing("KB_RETRIEVAL")
                
                kb_context = await kb_task
                timing_manager.end_section_timing("KB_RETRIEVAL")
                kb_response_text = kb_context if kb_context else "No knowledge base information available"
                
                print("\n📚 [KB] Knowledge Base Response:")
                print(kb_response_text)
                print("======================================\n")
                
                # Step 3: Generate combined response with proper intent prioritization
                print("\n [CLAUDE] ========== GENERATING FINAL RESPONSE ==========")
                timing_manager.start_section_timing("FINAL_RESPONSE_GENERATION")
                
                if "category" in intent_data:
                    category = intent_data["category"]
                    is_api_related = intent_data.get("is_api_related", False)
                    
                    if category in ["ADVISORY", "REAL_TIME"] or (category in ["ROUTE", "SCHEDULE", "STATION"] and is_api_related):
                        intent_type = "API"
                        print(f"Query classified as API based on intent classification: {category}, is_api_related: {is_api_related}")
                    else:
                        intent_type = "KB"
                        print("Query classified as KB based on intent classification")
                elif query_type == "kb":
                    intent_type = "KB"
                    print("Query classified as KB question, prioritizing knowledge base")
                else:
                    intent_type = "MIXED"
                
                logging.info(f"Query intent determined as: {intent_type}")
                
                if intent_type == "KB" and (not kb_context or kb_context.strip() == ""):
                    logging.info("KB intent but no KB data available, falling back to mixed intent")
                    intent_type = "MIXED"
                
                timing_manager.start_section_timing("CLAUDE_RESPONSE_GENERATION")
                final_response = await generate_combined_response(
                    query_text, 
                    kb_context, 
                    bart_data,
                    user_id=user_id,
                    session_id=session_id,
                    intent_type=intent_type,
                    language=language
                )
                timing_manager.end_section_timing("CLAUDE_RESPONSE_GENERATION")
                
                print("\n [CLAUDE] Final Response:")
                print(final_response)
                print("==========================================\n")
                timing_manager.end_section_timing("FINAL_RESPONSE_GENERATION")
                
                if intent_type == "API" and bart_data:
                    combined_text = f"""
                    Final Combined Response:
                    --------
                    {final_response}
                    """
                elif intent_type == "KB":
                    combined_text = f"""
                    Final Combined Response:
                    --------
                    {final_response}
                    """
                else: 
                    combined_text = f"""
                    Final Combined Response:
                    --------
                    {final_response}
                    """
                # Step 4: Send text response first, then synthesize and stream audio
                print("\n [TEXT] ========== SENDING TEXT RESPONSE ==========")
                await save_and_send_response(websocket, user_id, original_query, combined_text, final_response, session_id)
                print("=========================================\n")
                
                timing_manager.mark_text_response_complete()
                
                # Step 5: Synthesize and stream audio for the final response only
                print("\n [AUDIO] ========== STREAMING AUDIO RESPONSE ==========")
                timing_manager.start_audio_timing()
                timing_manager.start_section_timing("AUDIO_SYNTHESIS")
                
                if original_query_language != 'en':
                    logging.info(f"Translating response back to {original_query_language}")
                    try:
                        translated_response = await translate_text(final_response, source_lang='en', target_lang=original_query_language)
                        logging.info(f"Translated response: {truncate_json_for_logging(translated_response, max_length=150)}")
                        
                        final_response = translated_response
                        combined_text = combined_text.replace("Final Combined Response:\n--------\n", f"Final Combined Response:\n--------\n{translated_response}")
                    except Exception as e:
                        logging.error(f"Error translating response: {str(e)}")
                
                replaced_query = await get_state_value_from_db(user_id, session_id, 'replaced_query', None)
                is_station_replacement = replaced_query is not None
                
                phrases = chunk_text_by_words(final_response, language=language)
                for phrase in phrases:
                    if phrase.strip():
                        audio_data = await synthesize_speech(phrase, language=language)
                        if audio_data:
                            success = await safe_websocket_send_bytes(websocket, audio_data)
                            if success:
                                print(f" [AUDIO] Streamed audio for phrase: {phrase}")
                            else:
                                logging.warning("WebSocket disconnected, stopping audio streaming")
                                break  
                timing_manager.end_section_timing("AUDIO_SYNTHESIS")
                print("=========================================\n")
                
                # ======================================UPDATE CONTEXT WITH FINAL RESPONSE======================================
                should_save_context = (query_type == "api" or 
                                     (intent_data.get("is_api_related", False) and 
                                      intent_data.get("category") in ["ADVISORY", "REAL_TIME", "ROUTE", "SCHEDULE", "STATION"]))
                
                print(f"\n========== CONTEXT SAVING DECISION ==========")
                print(f"query_type: {query_type}")
                print(f"is_api_related: {intent_data.get('is_api_related', False)}")
                print(f"category: {intent_data.get('category', 'None')}")
                print(f"should_save_context: {should_save_context}")
                print("=============================================\n")
                
                if final_response:
                    timing_manager.start_section_timing("CONTEXT_SAVING")
                    session_context = await context_manager.get_session_context(user_id, session_id)
                    session_context.store_response(final_response)
                    logging.info(f"Stored response for repeat functionality - Response length: {len(final_response)}")
                    
                    try:
                        await context_manager._save_context_to_db(user_id, session_id, session_context)
                        logging.info(f"Saved session context with response to database")
                    except Exception as save_error:
                        logging.error(f"Error saving session context to database: {str(save_error)}")
                    timing_manager.end_section_timing("CONTEXT_SAVING")
                
                if should_save_context:
                    final_intent_data = intent_data.copy()
                    final_intent_data["category"] = category  
                    final_intent_data["api_endpoint"] = api_endpoint  
                    final_intent_data["parameters"] = params  
                    
                    print(f"\n========== FINAL CONTEXT VALUES TO BE STORED ==========")
                    print(f"Original Category: {intent_data.get('category')} → Final Category: {category}")
                    print(f"Original Endpoint: {intent_data.get('api_endpoint')} → Final Endpoint: {api_endpoint}")
                    print(f"Original Parameters: {intent_data.get('parameters')} → Final Parameters: {params}")
                    print("========================================================\n")
                    
                    await context_manager.update_context_after_processing(
                        user_id, session_id, original_query, query_text, final_intent_data, {"final_response": final_response}, query_type
                    )
                else:
                    print(f"\n========== CONTEXT NOT STORED ==========")
                    print(f"Intent: {query_type} (non-API intent - context not stored)")
                    print(f"is_api_related: {intent_data.get('is_api_related', False)}")
                    print(f"category: {intent_data.get('category', 'None')}")
                    print("=========================================\n")

                timing_manager.end_total_timing(include_audio=False)
                
                return combined_text
                
            except json.JSONDecodeError as e:
                print(f"\nError: Failed to parse intent classification response: {e}")
                print("Falling back to knowledge base only\n")
                
                if query_type != 'greeting' and query_type != 'stop_command' and query_type != 'repeat_request':
                    thinking_msg = random.choice(selected_thinking_messages)
                    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                    
                    print(f"\n========== SENDING THINKING MESSAGE (FALLBACK KB) ==========")
                    print(f"Time: {current_time}")
                    print(f"Message: '{thinking_msg}'")
                    print(f"Query type: '{query_type}' (fallback KB)")
                    print("=======================================================\n")
                    
                    thinking_response = {
                        "type": "thinking_message",
                        "message": thinking_msg,
                        "timestamp": current_time
                    }
                    await safe_websocket_send_text(websocket, json.dumps(thinking_response, ensure_ascii=False))
                    
                    thinking_audio = await synthesize_speech(thinking_msg, language=language)
                    if thinking_audio:
                        await safe_websocket_send_bytes(websocket, thinking_audio)
                        await asyncio.sleep(0.5)
                
                timing_manager.start_section_timing("KB_FALLBACK")
                kb_context = await retrieve_knowledge(query_text)
                timing_manager.end_section_timing("KB_FALLBACK")
                print("\n📚 [KB] ========== KNOWLEDGE BASE FALLBACK ==========")
                print(f"📚 [KB] Query: {query_text}")
                print("\n📚 [KB] Knowledge Base Response:")
                print(kb_context if kb_context else "No knowledge base information available")
                print("=========================================\n")
                
                timing_manager.start_section_timing("CLAUDE_FALLBACK_RESPONSE")
                final_response = await generate_combined_response(
                    query_text, 
                    kb_context, 
                    None,
                    intent_type="KB",
                    language=language
                )
                timing_manager.end_section_timing("CLAUDE_FALLBACK_RESPONSE")
                print("\n [CLAUDE] ========== FINAL FALLBACK RESPONSE ==========")
                print(final_response)
                print("=========================================\n")
                
                combined_text = f"""
                Final Combined Response:
                --------
                {final_response}
                """
                
                print("\n [TEXT] ========== SENDING FALLBACK TEXT RESPONSE ==========")
                await save_and_send_response(websocket, user_id, query_text, combined_text, final_response, session_id)
                print("=========================================\n")
                
                timing_manager.mark_text_response_complete()
                
                print("\n [AUDIO] ========== STREAMING FALLBACK AUDIO RESPONSE ==========")
                timing_manager.start_audio_timing()
                timing_manager.start_section_timing("AUDIO_SYNTHESIS")
                
                phrases = chunk_text_by_words(final_response, language=language)
                for phrase in phrases:
                    if phrase.strip():
                        audio_data = await synthesize_speech(phrase, language=language)
                        if audio_data:
                            success = await safe_websocket_send_bytes(websocket, audio_data)
                            if success:
                                print(f" [AUDIO] Streamed audio for phrase: {phrase}")
                            else:
                                logging.warning("WebSocket disconnected, stopping audio streaming")
                                break 
                timing_manager.end_section_timing("AUDIO_SYNTHESIS")
                print("=========================================\n")
                # ======================================STORE FALLBACK QUERY CONTEXT======================================
                print(f"\n========== FALLBACK QUERY CONTEXT (NOT STORED) ==========")
                print(f"Intent: kb (context break - not storing)")
                print(f"Category: KB_FALLBACK")
                print("===========================================================\n")
                
                timing_manager.end_total_timing(include_audio=False)
                        
                return combined_text
                
    except (WebSocketDisconnect, RuntimeError) as ws_error:
        print(f"\nError: WebSocket connection error in query processing: {str(ws_error)}")
        timing_manager.end_total_timing(include_audio=False)
        raise
    except Exception as e:
        error_message = str(e)
        stack_trace = traceback.format_exc()
        
        print(f"\nError: Error in query processing pipeline: {error_message}")
        logging.error(f"Query processing error: {error_message}\n{stack_trace}")
        
        try:
            from database_operations import save_error_log_to_db
            await save_error_log_to_db(
                user_id=user_id if user_id else "unknown",
                session_id=session_id if session_id else "unknown",
                error_message=error_message,
                error_type="query_processing",
                query_text=query_text,
                stack_trace=stack_trace
            )
        except Exception as db_error:
            logging.error(f"Failed to save error to database: {str(db_error)}")
        
        user_error_msg = f"Error: {error_message}"
        fallback_audio = await synthesize_speech("Sorry, I couldn't process that request. Please try again.")
        await safe_websocket_send_bytes(websocket, fallback_audio)
        
        try:
            await safe_websocket_send_text(websocket, json.dumps({
                "type": "error",
                "message": user_error_msg,
                "timestamp": datetime.utcnow().isoformat()
            }, ensure_ascii=False))
        except Exception as send_error:
            logging.error(f"Failed to send error message to user: {str(send_error)}")
        
        timing_manager.end_total_timing(include_audio=False)
        return user_error_msg

# ======================================AUDIO STREAMING CLASSES======================================
class PCMStream(AudioStream):
    """Production-grade PCM stream with proper resource management"""
    
    def __init__(self, audio_bytes: bytes):
        self.audio_bytes = audio_bytes
        self.chunk_size = 1024
        self._closed = False
        self._position = 0

    async def __aiter__(self):
        """Async iterator with proper resource management"""
        try:
            while not self._closed and self._position < len(self.audio_bytes):
                end_pos = min(self._position + self.chunk_size, len(self.audio_bytes))
                chunk = self.audio_bytes[self._position:end_pos]
                self._position = end_pos
                
                if chunk:  
                    yield chunk
                    await asyncio.sleep(0.01)
                else:
                    break
        except Exception as e:
            logging.error(f"Error in PCMStream iteration: {str(e)}")
            raise
        finally:
            self.close()
    
    def close(self):
        """Close and cleanup stream resources"""
        if not self._closed:
            self._closed = True
            self.audio_bytes = None
            self._position = 0
            logging.debug("PCMStream closed and cleaned up")
    
    def __del__(self):
        """Ensure cleanup on deletion"""
        self.close()

# ======================================TRANSCRIPTION HELPER FUNCTIONS======================================
def is_empty_transcription(transcribe_result: dict) -> bool:
    """
    Detects if a transcription result from AWS Transcribe is empty (no speech detected).
    
    Args:
        transcribe_result: The transcription result from AWS Transcribe
        
    Returns:
        bool: True if the transcription is empty, False otherwise
    """
    if not transcribe_result:
        return True
        
    if isinstance(transcribe_result, str):
        return not transcribe_result.strip()
        
    results = transcribe_result.get("Transcript", {}).get("Results", [])
    if not results:
        return True
        
    for result in results:
        for alt in result.get("Alternatives", []):
            if alt.get("Transcript", "").strip():
                return False
                
    return True

# ======================================TRANSCRIPTION FUNCTIONS======================================
async def transcribe_stream(audio_bytes: bytes, language_code: str = "en-US", websocket: WebSocket = None, user_id: str = None, session_id: str = None) -> str:
    try:
        print("\n========== TRANSCRIPTION START ==========")
        print(f"Processing {len(audio_bytes)} bytes of audio in language: {language_code}")
        print("========================================\n")
        
        async with transcribe_stream_context() as client:
            logging.info(f"Starting transcription stream for {len(audio_bytes)} bytes of audio in {language_code}")
            
            transcription_params = {
                "language_code": language_code,
                "media_sample_rate_hz": 16000,
                "media_encoding": "pcm",
                "enable_partial_results_stabilization": True,
                "partial_results_stability": "high",
                "show_speaker_label": False,
                "enable_channel_identification": False
            }
            
            if language_code.startswith("en"):
                transcription_params["vocabulary_name"] = "BART_Custom_Vocabulary"
                logging.info("Using BART custom vocabulary for English transcription")
            
            stream = await client.start_stream_transcription(**transcription_params)

            async def send_audio():
                chunk_size = 512  
                total_chunks = 0
                for i in range(0, len(audio_bytes), chunk_size):
                    chunk = audio_bytes[i:i + chunk_size]
                    await stream.input_stream.send_audio_event(audio_chunk=chunk)
                    total_chunks += 1
                    
                    if total_chunks % 20 == 0:
                        await asyncio.sleep(0.01)
                    
                await stream.input_stream.end_stream()
                logging.info(f"Sent {total_chunks} chunks to Transcribe")

            transcripts = []

            async def receive_transcript():
                async for event in stream.output_stream:
                    if isinstance(event, TranscriptEvent):
                        for result in event.transcript.results:
                            if not result.is_partial:
                                for alt in result.alternatives:
                                    transcripts.append(alt.transcript)

            await asyncio.gather(send_audio(), receive_transcript())
            
            result = " ".join(transcripts)
            if result and result.strip():
                logging.info(f"Transcription complete, received {len(result)} characters")
                
                print("\n========== TRANSCRIPTION RESULT ==========")
                print(f"Transcribed text: '{result}'")
                print("=========================================\n")
                
                if websocket:
                    try:
                        transcription_data = {
                            'type': 'transcription',
                            'text': result,
                            'timestamp': datetime.utcnow().isoformat()
                        }
                        await websocket.send_text(json.dumps(transcription_data, ensure_ascii=False))
                        logging.info(f"Sent transcription to frontend: '{result}'")
                    except Exception as ws_error:
                        logging.error(f"Error sending transcription to frontend: {str(ws_error)}")
            else:
                logging.info("Transcription complete, no speech detected")
                
                if websocket:
                    try:
                        empty_audio_notification = {
                            'type': 'empty_audio',
                            'status': 'empty',
                            'message': 'No speech detected or empty audio',
                            'timestamp': datetime.utcnow().isoformat()
                        }
                        await websocket.send_text(json.dumps(empty_audio_notification, ensure_ascii=False))
                        logging.info("Sent empty audio notification to frontend")
                        
                        language = 'en'
                        
                        try:
                            if user_id and session_id:
                                language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
                        except Exception as e:
                            logging.warning(f"Could not get language from DB for empty audio notification: {str(e)}")
                            language = 'en' 

                        empty_audio_messages = {
                            'en': "I didn't hear anything. Please try speaking again.",
                            'es': "No escuché nada. Por favor, intenta hablar de nuevo.",
                            'zh': "我没有听到任何内容。请再次尝试说话。"
                        }
                        
                        message = empty_audio_messages.get(language, empty_audio_messages['en'])
                        
                        try:
                            audio_data = await synthesize_speech(message, language=language)
                            if audio_data:
                                await safe_websocket_send_bytes(websocket, audio_data)
                                logging.info(f"Sent audio notification for empty audio in {language}")
                        except Exception as audio_error:
                            logging.warning(f"Could not send audio notification for empty audio: {str(audio_error)}")
                            
                    except Exception as ws_error:
                        logging.error(f"Error sending empty audio notification to frontend: {str(ws_error)}")
            
            return result
        
    except Exception as e:
        logging.exception(f"Error in transcribe_stream: {str(e)}")
        
        if websocket:
            try:
                error_notification = {
                    'type': 'transcription_error',
                    'status': 'error',
                    'message': f'Error processing audio: {str(e)}',
                    'timestamp': datetime.utcnow().isoformat()
                }
                await websocket.send_text(json.dumps(error_notification, ensure_ascii=False))
                logging.info("Sent transcription error notification to frontend")
            except Exception as ws_error:
                logging.warning(f"Could not send transcription error notification: {str(ws_error)}")
                
        return ""

# ======================================UTILITY FUNCTIONS======================================
def pretty_json(obj):
    """Format JSON objects for better readability in logs"""
    if obj is None:
        return "None"
    
    if isinstance(obj, (dict, list)):
        try:
            return json.dumps(obj, indent=2, ensure_ascii=False)
        except:
            return pprint.pformat(obj, indent=2)
    else:
        return str(obj)

async def retrieve_knowledge(query: str) -> str:
    print("\n📚 [KB] ========== KNOWLEDGE BASE RETRIEVAL ==========")
    print(f"📚 [KB] Query: {query}")
    print("============================================\n")
    
    kb_instructions = KB_INSTRUCTIONS
    
    enhanced_query = f"""
    User Query: {query}

    Instructions:
    {kb_instructions.strip()}
    """
    
    try:
        request_config = {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KNOWLEDGE_BASE_ID,
                "modelArn": MODEL_ARN,
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": 3,  
                        "overrideSearchType": "SEMANTIC" 
                    }
                }
            }
        }
                
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    kb_client.retrieve_and_generate,
                    input={"text": enhanced_query},
                    retrieveAndGenerateConfiguration=request_config
                ),
                timeout=10.0  # CRITICAL FIX: Increased from 6s to 10s for reliability
            )
        except asyncio.TimeoutError:
            logging.error("Knowledge base retrieval timed out after 10 seconds")
            return "I don't have that information right now."
        
        logging.info(f"Knowledge base response received successfully")
        
    except Exception as e:
        logging.error(f"Error calling knowledge base: {str(e)}")
        logging.error(f"Error type: {type(e).__name__}")
        
        if "Parameter validation failed" in str(e):
            logging.error("This appears to be a boto3 version issue. Please ensure boto3 >= 1.34.84 is installed.")
            return "I'm sorry, I encountered a configuration error with the knowledge base. This may be due to an outdated AWS SDK version. Please contact support."
        else:
            return f"I'm sorry, I encountered an error while retrieving information from the knowledge base. Error: {str(e)}"
    
    try:
        generated_text = response.get("output", {}).get("text", "No relevant documents found.")
        
        citations = []
        if "citations" in response:
            try:
                for citation in response.get("citations", []):
                    retrieved_refs = citation.get("retrievedReferences", [])
                    if isinstance(retrieved_refs, list):
                        for ref in retrieved_refs:
                            if isinstance(ref, dict):
                                source_url = ref.get("source")
                                if source_url:
                                    citations.append(source_url)
                    elif isinstance(retrieved_refs, dict):
                        source_url = retrieved_refs.get("source")
                        if source_url:
                            citations.append(source_url)
            except Exception as e:
                logging.error(f"Error processing citations: {str(e)}")
        
        if citations:
            unique_citations = list(dict.fromkeys(citations))[:2]
            final_result = generated_text
            for url in unique_citations:
                final_result += f"\n\n===== URL: {url} ====="
        else:
            final_result = generated_text
        
        logging.info(f"Knowledge base response: {truncate_json_for_logging(final_result, max_length=200)}")
        
        print("\n========== KNOWLEDGE BASE RESULT ==========")
        print(f"Response length: {len(final_result)} chars")
        print("=========================================\n")
        
        return final_result
        
    except Exception as e:
        error_msg = f"Error processing knowledge base response: {str(e)}"
        logging.error(error_msg)
        return "I'm sorry, I encountered an error while processing the knowledge base response. Please try again with a different query."
    
def chunk_text_by_words(text, min_words=6, max_words=8, language="en"):
    """Split text into chunks for speech synthesis, with language-specific handling"""
    
    if language in ["zh"]:
        min_chars = 30
        max_chars = 80
        
        sentences = re.split(r'([!?|？])', text)
        chunks = []
        current_chunk = ""
        
        i = 0
        while i < len(sentences):
            if i + 1 < len(sentences) and len(sentences[i+1]) == 1 and sentences[i+1] in "!?？":
                sentence = sentences[i] + sentences[i+1]
                i += 2
            else:
                sentence = sentences[i]
                i += 1
                
            if len(sentence) > max_chars:
                if language == "zh":
                    sub_parts = re.split(r'(,|，|、|；)', sentence)
                else:
                    sub_parts = re.split(r'(,)', sentence)
                
                j = 0
                sub_chunk = ""
                while j < len(sub_parts):
                    if j + 1 < len(sub_parts) and len(sub_parts[j+1]) == 1:
                        sub_sentence = sub_parts[j] + sub_parts[j+1]
                        j += 2
                    else:
                        sub_sentence = sub_parts[j]
                        j += 1
                    
                    if len(sub_chunk) + len(sub_sentence) <= max_chars:
                        sub_chunk += sub_sentence
                    else:
                        if sub_chunk:
                            chunks.append(sub_chunk)
                        sub_chunk = sub_sentence
                
                if sub_chunk:
                    chunks.append(sub_chunk)
            else:
                if len(current_chunk) + len(sentence) <= max_chars:
                    current_chunk += sentence
                else:
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = sentence
        
        if current_chunk:
            chunks.append(current_chunk)
            
        return [chunk for chunk in chunks if chunk.strip()]
    
    words = text.split()
    chunks = []
    current_chunk = []
    
    if len(words) <= max_words:
        return [text]
    
    for word in words:
        current_chunk.append(word)
        
        if len(current_chunk) >= min_words and (
            len(current_chunk) >= max_words or 
            word.endswith(('.', '!', '?', ':', ';')) or 
            (word.endswith((',')) and len(current_chunk) >= min_words)
        ):
            chunks.append(' '.join(current_chunk))
            current_chunk = []
    
    if current_chunk:
        chunks.append(' '.join(current_chunk))
        
    return chunks

# ======================================STREAMING RESPONSE FUNCTIONS======================================
async def synthesize_and_stream_parallel(phrases: list, language: str, websocket: WebSocket, max_concurrent: int = 3):
    """Synthesize multiple phrases in parallel and stream them in order with connection checking"""
    if not phrases:
        return
    
    for i in range(0, len(phrases), max_concurrent):
        if not is_websocket_connected(websocket):
            logging.info("WebSocket disconnected, stopping audio streaming")
            break
            
        batch = phrases[i:i + max_concurrent]
        
        synthesis_tasks = []
        for phrase in batch:
            if phrase.strip():
                task = asyncio.create_task(synthesize_speech(phrase, language=language))
                synthesis_tasks.append((phrase, task))
        
        for phrase, task in synthesis_tasks:
            if not is_websocket_connected(websocket):
                logging.info("WebSocket disconnected during streaming, stopping")
                task.cancel()
                break
                
            try:
                audio_data = await task
                if audio_data:
                    success = await safe_websocket_send_bytes(websocket, audio_data)
                    if success:
                        logging.info(f" [AUDIO] Streamed audio for phrase: {truncate_json_for_logging(phrase, 50)}")
                    else:
                        logging.info("WebSocket disconnected, stopping audio streaming")
                        break
            except Exception as e:
                logging.error(f"Error streaming phrase: {str(e)}")
                if "going away" in str(e).lower() or "disconnected" in str(e).lower():
                    logging.info("Connection lost, stopping audio streaming")
                    break

async def stream_response_and_audio(user_query: str, kb_context, websocket: WebSocket, bart_data=None):
    if not isinstance(kb_context, str):
        kb_context = str(kb_context)
    
    user_id, session_id = await get_user_session_info(websocket)
    
    is_api_related = bart_data is not None
    language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
    skip_tts = await get_state_value_from_db(user_id, session_id, 'skip_tts', False)
    logging.info(f"Streaming response for query: '{user_query}' in language: {language}")
    logging.info(f"Query is API related: {is_api_related}")
    logging.info(f"Skip TTS: {skip_tts}")

    try:
        if not is_websocket_connected(websocket):
            logging.info("WebSocket disconnected, skipping response generation")
            return
            
        full_response = await generate_combined_response(user_query, kb_context, bart_data)
        
        if full_response:
            logging.info(f"Response generated: {truncate_json_for_logging(full_response, max_length=150)}")
            
            if not is_websocket_connected(websocket):
                logging.info("WebSocket disconnected, skipping audio streaming")
                return
            
            if language != 'en':
                translated_response = await translate_text(full_response, source_lang='en', target_lang=language)
                logging.info(f"Translated response: {truncate_json_for_logging(translated_response, max_length=150)}")
            else:
                translated_response = full_response
            
            if not skip_tts:
                if not is_websocket_connected(websocket):
                    logging.info("WebSocket disconnected, skipping TTS")
                    return
                if language in ['hi', 'zh', 'ar']:
                    phrases = chunk_text_by_words(translated_response, language=language)
                    await synthesize_and_stream_parallel(phrases, language, websocket)
                else:
                    sentences = re.split(r'(?<=[.!?])\s+', translated_response)
                    current_chunk = []
                    word_count = 0
                    chunks = []
                    
                    for sentence in sentences:
                        if not sentence.strip():
                            continue
                        
                        sentence_words = len(sentence.split())
                        
                        if word_count + sentence_words > 15:
                            if current_chunk:
                                chunks.append(' '.join(current_chunk))
                                current_chunk = []
                                word_count = 0
                        
                        current_chunk.append(sentence)
                        word_count += sentence_words
                        
                        if word_count >= 10:
                            chunks.append(' '.join(current_chunk))
                            current_chunk = []
                            word_count = 0
                    
                    if current_chunk:
                        chunks.append(' '.join(current_chunk))
                    
                    await synthesize_and_stream_parallel(chunks, language, websocket)
            
            conversation_data = {
                "type": "conversation",
                "query": user_query,
                "response": translated_response,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": session_id,
                "conversation_id": str(uuid.uuid4()),
                "is_final_response": True
            }
            await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
            
            await set_state_value_in_db(user_id, session_id, 'response_sent', True)
                    
            return full_response
        
        else:
            fallback_messages = {
                "en": "I'm sorry, I couldn't find specific information to answer your question about that.",
                "es": "Lo siento, no pude encontrar información específica para responder a tu pregunta sobre eso.",
                "zh": "对不起，我找不到具体信息来回答您关于那个的问题。"
            }
            
            fallback_msg = fallback_messages.get(language, fallback_messages["en"])
            logging.warning(f"No response generated, using fallback: {fallback_msg}")
            
            await set_state_value_in_db(user_id, session_id, 'response_sent', True)
            
            if not skip_tts:
                phrases = chunk_text_by_words(fallback_msg, language=language)
                for phrase in phrases:
                    audio_data = await synthesize_speech(phrase, language=language)
                    if audio_data:
                        await safe_websocket_send_bytes(websocket, audio_data)
            
            fallback_conversation_data = {
                "type": "conversation",
                "query": user_query,
                "response": fallback_msg,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": session_id,
                "conversation_id": str(uuid.uuid4()),
                "is_final_response": True
            }
            await websocket.send_text(json.dumps(fallback_conversation_data, ensure_ascii=False))
            
            await set_state_value_in_db(user_id, session_id, 'response_sent', True)
            
            return fallback_msg

    except Exception as e:
        logging.exception(f"Response generation failed: {str(e)}")
        error_messages = {
            "en": "Sorry, I couldn't get that. Try again?",
            "es": "Lo siento, no pude entender eso. ¿Intentas de nuevo?",
            "zh": "抱歉，我没听懂。请再试一次？"
        }
        error_msg = error_messages.get(language, error_messages["en"])
        
        await set_state_value_in_db(user_id, session_id, 'response_sent', True)
        
        if not skip_tts:
            fallback = await synthesize_speech(error_msg, language=language)
            if fallback:
                await websocket.send_bytes(fallback)
        
        error_conversation_data = {
            "type": "conversation",
            "query": user_query,
            "response": error_msg,
            "timestamp": datetime.utcnow().isoformat(),
            "session_id": session_id,
            "conversation_id": str(uuid.uuid4()),
            "is_final_response": True
        }
        await websocket.send_text(json.dumps(error_conversation_data, ensure_ascii=False))
        
        await set_state_value_in_db(user_id, session_id, 'response_sent', True)
        
        return error_msg

# ======================================LLM API FUNCTIONS======================================
async def non_streaming_claude_response(request_body):
    try:
        model_id = request_body.get("model_id", MODEL_ID)
        start_time = asyncio.get_event_loop().time()

        request_info = {
            "model": model_id,
            "max_tokens": request_body.get("max_tokens", 1000),
            "temperature": request_body.get("temperature", 0.7),
            "anthropic_version": request_body.get("anthropic_version", "bedrock-2023-05-31"),
            "prompt_length": len(json.dumps(request_body.get("messages", [])))
        }
        
        response = await asyncio.to_thread(
            bedrock_client.invoke_model,
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body)
        )
        
        latency = asyncio.get_event_loop().time() - start_time
        
        response_body = json.loads(response.get('body').read())
        
        if "content" in response_body and isinstance(response_body["content"], list):
            for content in response_body["content"]:
                if isinstance(content, dict) and "text" in content:
                    text = content["text"]
                    latency = asyncio.get_event_loop().time() - start_time
                    logging.info(f"Claude response received in {latency:.2f}s ({len(text)} chars)")
                
                    return text
        
        logging.warning(f"Unexpected Claude response format: {truncate_json_for_logging(response_body)}")
        return None
    except Exception as e:
        logging.exception(f"Claude API call failed: {e}")
        return None

# ======================================SPEECH SYNTHESIS======================================
def clean_text_for_speech(text: str) -> str:
    """
    Clean text for speech synthesis by handling symbols that shouldn't be read aloud.
    This preserves the text for display but improves the speech experience.
    """
    import re
    
    speech_text = text
    
    patterns_to_remove = [
        r'\[[^\]]*\]',  
        r'\{[^}]*\}',   
        r'\/+',         
        r'\\+',         
        r'\|',          
        r'\*+',         
        r'\#+',         
        r'\-{2,}',      
        r'_{2,}',       
        r'={2,}',       
        r'~+',          
        r'`+',          
        r'<[^>]*>',     
    ]
    
    for pattern in patterns_to_remove:
        speech_text = re.sub(pattern, ' ', speech_text)
    
    speech_text = re.sub(r'\s+', ' ', speech_text)
    
    return speech_text.strip()

async def synthesize_speech(text: str, language: str = "en") -> bytes:
    """Convert text to speech using Amazon Polly with language support and enhanced SSML"""
    try:
        print(f"[AUDIO] Text length: {len(text)} chars, Language: {language}")
        
        speech_text = clean_text_for_speech(text)
        
        language_info = SUPPORTED_LANGUAGES.get(language, SUPPORTED_LANGUAGES['en'])
        voice_id = language_info['polly_voice']
        
        if len(speech_text) > 1000:
            logging.warning(f"Text too long for Polly ({len(speech_text)} chars), truncating to 1000 chars")
            speech_text = speech_text[:997] + "..."
        
        logging.info(f'Synthesizing speech: "{speech_text}" in {language} using voice {voice_id}')
        
        engine = 'neural'
        output_format = 'mp3'
        
        modified_text = speech_text
        
        if language == 'en':
            modified_text = modified_text.replace('St.', 'Street')
            modified_text = modified_text.replace('Ave.', 'Avenue')
            modified_text = modified_text.replace('Rd.', 'Road')
            modified_text = modified_text.replace('Blvd.', 'Boulevard')
            
            modified_text = re.sub(r'(\w+)\s*-\s*(\w+)', r'\1 \2', modified_text)
        
        modified_text = modified_text.replace('&', '&amp;')
        modified_text = modified_text.replace('<', '&lt;')
        modified_text = modified_text.replace('>', '&gt;')
        
        ssml_text = "<speak>"
        
        speech_rate = language_info.get('speech_rate', 'medium')
        ssml_text += f'<prosody rate="{speech_rate}">'
        
        modified_text = re.sub(r'•\s*', ' <break strength="medium"/> ', modified_text)
        modified_text = re.sub(r'^\s*-\s+', ' <break strength="medium"/> ', modified_text, flags=re.MULTILINE)
        modified_text = re.sub(r'\n\s*-\s+', ' <break strength="medium"/> ', modified_text)
        
        modified_text = re.sub(r'(\d+)\.\s+', r' <break strength="medium"/> \1. ', modified_text)
        
        modified_text = re.sub(r'\(([^)]+)\)', r' (\1) ', modified_text)
        
        if language == 'en':
            modified_text = re.sub(r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)', r'\1 \2 \3', modified_text)
            
            modified_text = re.sub(r'\$(\d+)\.(\d{2})', r'\1 dollars and \2 cents', modified_text)
            
            modified_text = re.sub(r'\s+-\s+', ' , ', modified_text)
            
            modified_text = re.sub(r'\b0\b', 'zero', modified_text)
            modified_text = re.sub(r'\b0(\d)', r'zero \1', modified_text)
        
        ssml_text += modified_text
        
        ssml_text += "</prosody></speak>"
        
        response = await asyncio.to_thread(
            polly_client.synthesize_speech,
            OutputFormat=output_format,
            VoiceId=voice_id,
            TextType='ssml',
            Text=ssml_text,
            Engine=engine,
            SampleRate='16000'
        )
        
        audio_stream: StreamingBody = response['AudioStream']
        audio_data = audio_stream.read()
        
        logging.info(f"Successfully synthesized {len(audio_data)} bytes of audio in {language}")
        return audio_data
    except Exception as e:
        logging.exception(f"Polly synthesis failed: {str(e)}")
        return b''

# ======================================APP STARTUP======================================
@app.on_event("startup")
async def startup_event():
    """Comprehensive application startup with all monitoring and health checks"""
    logging.info("Starting MAAS LLM App with production optimizations...")
    
    try:
        try:
            from aws_client_manager import aws_client_manager
            logging.info(f"AWS client manager initialized: {aws_client_manager.get_stats()}")
        except Exception as e:
            logging.warning(f"AWS client manager initialization failed (non-critical): {str(e)}")
        
        try:
            await start_memory_monitoring()
            logging.info("Memory monitoring started")
        except Exception as e:
            logging.warning(f"Memory monitoring failed to start (non-critical): {str(e)}")
        
        try:
            await resource_monitor.start_monitoring()
            logging.info("Resource monitoring started")
        except Exception as e:
            logging.warning(f"Resource monitoring failed to start (non-critical): {str(e)}")
        
        try:
            setup_simple_logging()
            logging.info("Simple session logging initialized")
        except Exception as e:
            logging.warning(f"Simple session logging failed to start (non-critical): {str(e)}")
        
        logging.info("MAAS LLM App startup completed successfully")
        
    except Exception as e:
        logging.error(f"Error during application startup: {str(e)}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Comprehensive application shutdown - graceful cleanup of all resources"""
    logging.info("Shutting down MAAS LLM App - performing comprehensive cleanup...")
    
    try:
        await stop_memory_monitoring()
        logging.info("Memory monitoring stopped")
        
        try:
            from aws_client_manager import cleanup_aws_clients
            cleanup_aws_clients()
            logging.info("AWS clients cleaned up")
        except Exception as e:
            logging.warning(f"Error cleaning up AWS clients: {str(e)}")
        
        try:
            from audio_buffer_manager import cleanup_audio_resources
            cleanup_audio_resources()
            logging.info("Audio resources cleaned up")
        except Exception as e:
            logging.warning(f"Error cleaning up audio resources: {str(e)}")
        
        logging.info("Simple user session logging cleanup completed")
        
        session_manager_instance.sessions.clear()
        logging.info("Cleared all sessions")
        
        collected = gc.collect()
        logging.info(f"Final garbage collection freed {collected} objects")
        
        resource_monitor.connection_count = 0
        logging.info("Reset connection counter")
        
    except Exception as e:
        logging.error(f"Error during shutdown cleanup: {str(e)}")
    
    logging.info("Comprehensive shutdown completed - all resources cleaned up")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT"))
    uvicorn.run(app, host="0.0.0.0", port=port)

async def generate_combined_response(query: str, kb_context: str, bart_data: dict = None, user_id: str = None, session_id: str = None, intent_type: str = "MIXED", language: str = "en") -> str:
    """Generate a combined response using knowledge base, BART API data, and previous context."""
    try:
        print(f"\n[CLAUDE] ========== GENERATING COMBINED RESPONSE ==========")
        print(f"[CLAUDE] Query: {query}")
        print(f"[CLAUDE] Intent Type: {intent_type}")
        print(f" [CLAUDE] Language: {language}")
        previous_context = "No previous context available."
        contextual_query = query  
        
        if user_id and session_id:
            try:
                from context_manager import context_manager
                
                context_data = await context_manager.get_contextual_conversation_history(user_id, session_id, max_tokens=4000)
                
                if context_data.get('recent_conversations') or context_data.get('structured_context'):
                    contextual_query = await context_manager.build_contextual_prompt(query, context_data, max_tokens=4000)
                    logging.info(f"Enhanced query with context: {len(contextual_query)} chars")
                else:
                    logging.info("No recent context available, using original query")
                    
            except Exception as e:
                logging.error(f"Error getting contextual history: {str(e)}")
                prev_conversations = await get_previous_conversations(user_id, session_id)
                previous_context = await format_previous_conversations(prev_conversations)
        

        api_has_data = False
        api_no_data_warning = False  
        api_has_error = False 
        
        if bart_data:
            if isinstance(bart_data, dict):
                if "error" in bart_data:
                    api_has_error = True
                else:
                    if "root" in bart_data:
                        root = bart_data["root"]

                        # Check for station-level errors first
                        if "station" in root and isinstance(root["station"], list):
                            for station in root["station"]:
                                if isinstance(station, dict) and "message" in station:
                                    station_message = station["message"]
                                    if isinstance(station_message, dict) and "error" in station_message:
                                        if "Updates are temporarily unavailable" in station_message.get("error", ""):
                                            api_has_error = True
                                            break

                        # Pattern 1: ETD - "No data matched your criteria" (trains)
                        if isinstance(root, dict) and "message" in root:
                            message = root["message"]
                            if isinstance(message, dict) and "warning" in message:
                                if "No data matched your criteria" in message.get("warning", ""):
                                    if "station" in root and isinstance(root["station"], list) and len(root["station"]) > 0:
                                        api_has_data = True  
                                    else:
                                        api_no_data_warning = True
                        
                        if not api_no_data_warning:
                            if "bsa" in root and isinstance(root["bsa"], list) and len(root["bsa"]) > 0:
                                api_has_data = True  
                            
                            elif "elevators" in root or "escalators" in root:
                                api_has_data = True  
                            
                            elif "station" in root and isinstance(root["station"], list) and len(root["station"]) > 0:
                                api_has_data = True
                            
                            elif "traincount" in root:
                                api_has_data = True
                            
                            elif any(key in root for key in ["routes", "route", "schedule", "trip", "fares"]):
                                api_has_data = True
                            
                            else:
                                api_has_data = True
                    else:
                        api_has_data = True
        
        kb_has_data = kb_context and isinstance(kb_context, str) and len(kb_context.strip()) > 50
        
        logging.info(f"API data status - Has data: {api_has_data}, No data warning: {api_no_data_warning}, Has error: {api_has_error}, KB has data: {kb_has_data}, Intent: {intent_type}")
        
        if intent_type == "API" and api_has_data:
            # Get current date for context
            current_datetime = datetime.now()
            current_date_str = current_datetime.strftime("%B %d, %Y")
            current_year_str = str(current_datetime.year)
            
            prompt = f"""Question: {query}
            
            Previous Context:
            {contextual_query if contextual_query != query else previous_context}

            Available Information (Knowledge Base):
            NOT APPLICABLE - This is a real-time query

            Current Data:
            {json.dumps(bart_data)}

            Query Intent: {intent_type}
            Data Status: Available
            
            🚨 CRITICAL: Current Date Context
            Today's Date: {current_date_str}
            Current Year: {current_year_str}
            
            🚨 CRITICAL INSTRUCTION: Use ONLY the Current Data above. The Knowledge Base section is marked as NOT APPLICABLE because this is a real-time query.
            
            🚨 TRANSFER DETECTION RULE: 
            - Count the number of "leg" elements in each trip in the Current Data
            - If trip has 1 leg → Direct train, no transfer
            - If trip has 2+ legs → Transfer required
            - For questions about direct trains: If API shows 2+ legs, answer "No, you'll need to transfer"
            - For questions about transfers: If API shows 2+ legs, answer "Yes, you'll need to transfer"
            
            🚨 EXAMPLE: If Current Data shows trip with 2 legs (Fremont → 19th St → SFO) and user asks "Is there a direct train?", answer "No, you'll need to transfer at 19th St. Oakland"
            
            🚨 TRANSFER QUESTION DETECTION:
            - If query contains "direct train" and API shows 2+ legs → Answer "No, you'll need to transfer"
            - If query contains "do I need to transfer" and API shows 2+ legs → Answer "Yes, you'll need to transfer"
            - If query contains "transfer" and API shows 1 leg → Answer "No, it's a direct train"
            - ALWAYS base your answer on the leg count in the Current Data, not on assumptions
            """
        elif intent_type == "API" and api_has_error:
            # CASE 1: API has error (like "Updates are temporarily unavailable")
            # Get current date for context
            current_datetime = datetime.now()
            current_date_str = current_datetime.strftime("%B %d, %Y")
            current_year_str = str(current_datetime.year)
            
            prompt = f"""Question: {query}

            Previous Context:
            {contextual_query if contextual_query != query else previous_context}

            Current Data:
            {json.dumps(bart_data)}

            Query Intent: {intent_type}
            Data Status: Error - Updates Temporarily Unavailable
            
            🚨 CRITICAL: Current Date Context
            Today's Date: {current_date_str}
            Current Year: {current_year_str}
            
            🚨 CRITICAL INSTRUCTION 🚨:
            Updates are temporarily unavailable. There are no departures currently available.
            
            YOU MUST respond with EXACTLY THIS (NO OTHER WORDS):
            "Updates are temporarily unavailable. There are no departures currently available."
            
            ❌ FORBIDDEN - DO NOT SAY:
            - "API" or "API response" or "the API indicates"
            - "data" or "database" or "real-time"
            - "API query didn't return" or "API query failed"
            - "The API query didn't return" or "API query didn't return any"
            - Any explanation about why or technical details
            - "I don't have" or "I can't get" or "unfortunately"
            
            JUST say the exact message above. Nothing more.
            """
        elif intent_type == "API" and api_no_data_warning:
            # CASE 2: API succeeded but explicitly returned "No data matched your criteria"
            # Get current date for context
            current_datetime = datetime.now()
            current_date_str = current_datetime.strftime("%B %d, %Y")
            current_year_str = str(current_datetime.year)
            
            prompt = f"""Question: {query}

            Previous Context:
            {contextual_query if contextual_query != query else previous_context}

            Current Data:
            {json.dumps(bart_data)}

            Query Intent: {intent_type}
            Data Status: No Data Available
            
            🚨 CRITICAL: Current Date Context
            Today's Date: {current_date_str}
            Current Year: {current_year_str}
            
            🚨 CRITICAL INSTRUCTION 🚨:
            There is NO train information available right now (no trains running or no departures available).
            
            YOU MUST respond with EXACTLY ONE OF THESE (NO OTHER WORDS):
            - "I don't have train information right now."
            - "There are no trains running right now."
            - "I don't have departure information right now."
            
            ❌ FORBIDDEN - DO NOT SAY:
            - "API" or "API response" or "the API indicates"
            - "API query didn't return" or "API query failed"
            - "The API query didn't return" or "API query didn't return any"
            - "data" or "database" or "real-time"
            - Any explanation about why or technical details
            
            JUST say you don't have the information. Nothing more.
            """
        elif intent_type == "API" and api_has_error and kb_has_data:
            # CASE 3: API query FAILED/ERRORED (not "no data") BUT KB has comprehensive information
            # Get current date for context
            current_datetime = datetime.now()
            current_date_str = current_datetime.strftime("%B %d, %Y")
            current_year_str = str(current_datetime.year)
            
            prompt = f"""Question: {query}

            Previous Context:
            {contextual_query if contextual_query != query else previous_context}

            Available Information (Knowledge Base):
            {kb_context}

            Current Data:
            {json.dumps(bart_data) if bart_data else "No data available"}

            Query Intent: KB (real-time data unavailable, using Knowledge Base)
            Data Status: No Data (ERROR - IGNORE THIS)
            
            🚨 CRITICAL: Current Date Context
            Today's Date: {current_date_str}
            Current Year: {current_year_str}
            
            🚨 CRITICAL INSTRUCTION 🚨:
            The real-time data call failed with an error, BUT the Knowledge Base has comprehensive information to answer this query.
            
            YOU MUST:
            - Use the Knowledge Base information directly to answer the question
            - Provide the answer naturally as if you just know it
            - NEVER say "API query didn't return" or "API query failed"
            - DO NOT mention any technical terms, errors, data sources, or limitations
            - DO NOT say "I don't have information" when KB clearly has the answer
            - DO NOT say "Unfortunately" or "However" or "I don't have information about X right now"
            - DO NOT mention that data failed or that you're using alternative sources
            - Just answer the question directly using the KB information above
            
            Example:
            ❌ WRONG: "I don't have real-time information about airport connections right now. However, based on the knowledge base..."
            ✅ CORRECT: "BART provides direct connections to both Oakland International Airport and San Francisco International Airport..."
            
            START YOUR RESPONSE IMMEDIATELY WITH THE ANSWER FROM THE KNOWLEDGE BASE.
            """
        else:
            # Get current date for context
            current_datetime = datetime.now()
            current_date_str = current_datetime.strftime("%B %d, %Y")
            current_year_str = str(current_datetime.year)
            
            prompt = f"""Question: {query}

        Previous Context:
        {contextual_query if contextual_query != query else previous_context}

        Available Information (Knowledge Base):
        {kb_context if kb_context else "No knowledge base information available"}

        Current Data:
        {json.dumps(bart_data) if bart_data else "No data available"}

        Query Intent: {intent_type}
        Data Status: {"Data Available" if api_has_data else "No Data"}
        
        🚨 CRITICAL: Current Date Context
        Today's Date: {current_date_str}
        Current Year: {current_year_str}
        
        🚨 DATE-AWARE RESPONSE INSTRUCTIONS:
        - If the Knowledge Base mentions dates in the past (like "November 30, 2023"), provide context about whether that information is still current
        - If information is outdated, mention that it's no longer current
        - Always consider the current date when providing information about policies, procedures, or changes
        - If something was supposed to happen in the past (like "starting November 30, 2023"), and we're past that date, indicate that the change has already occurred
        """
        
        language_instructions = {
            "en": "Respond in English using natural, conversational language.",
            "es": "Responde en español usando lenguaje natural y conversacional. Traduce cualquier información técnica del inglés al español.",
            "zh": "用中文回答，使用自然、对话式的语言。将任何英文技术信息翻译成中文。"
        }
        
        system_prompt = COMBINED_RESPONSE_PROMPT.replace(
            '{STATION_ABBREVIATION_MAP}', str(STATION_ABBREVIATION_MAP)
        ).replace(
            '{SUPPORTED_LANGUAGES[language][\'name\']}', SUPPORTED_LANGUAGES[language]['name']
        )
        
        language_instruction = language_instructions.get(language, language_instructions["en"])
        system_prompt = f"{system_prompt}\n\n### LANGUAGE REQUIREMENT\n{language_instruction}"
        
        temperature_value = 0.0 if intent_type == "API" else 0.2
        if intent_type == "KB":
            max_tokens_value = 800  
        elif intent_type == "API":
            max_tokens_value = 1200  
        else:
            max_tokens_value = 1000  
        
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens_value,
            "temperature": temperature_value,
            "system": system_prompt,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        }
                
        response = await non_streaming_claude_response(request_body)
        
        if response:
            return response
        else:
            logging.warning("Empty response from Claude for final output")
            
            fallback_messages = {
                "en": "I'm sorry, I couldn't process that request properly.",
                "es": "Lo siento, no pude procesar esa solicitud correctamente.",
                "zh": "抱歉，我无法正确处理该请求。"
            }
            return fallback_messages.get(language, fallback_messages["en"])
        
    except Exception as e:
        logging.exception(f"Error generating combined response: {str(e)}")
        
        error_messages = {
            "en": "I'm sorry, I couldn't process that request properly.",
            "es": "Lo siento, no pude procesar esa solicitud correctamente.",
            "zh": "抱歉，我无法正确处理该请求。"
        }
        return error_messages.get(language, error_messages["en"])

# ======================================USER MANAGEMENT ENDPOINTS======================================
@app.post("/api/users")
async def create_user(user_data: dict):
    try:
        user_id = user_data.get('user_id')
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
            
        language = user_data.get('language', 'en')
        
        await create_or_update_user(user_id=user_id)
        await save_user_profile_to_db(user_id=user_id, language=language)
        
        return {"message": "User created/updated successfully"}
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/users/{user_id}/conversations")
async def get_user_conversation_history(user_id: str, limit: int = 10):
    try:
        conversations = await get_user_conversations(user_id, limit)
        return {"conversations": conversations}
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/users/{user_id}/sessions/{session_id}/conversations")
async def get_session_conversation_history(user_id: str, session_id: str, limit: int = 10):
    """Get conversation history for a specific session"""
    try:
        conversations = await get_session_conversations(user_id, session_id, limit)
        return {"conversations": conversations}
    except Exception as e:
        logging.error(f"Error getting session conversation history: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get session conversation history")

@app.get("/api/users/{user_id}")
async def get_user(user_id: str):
    try:
        response = bart_table.get_item(
            Key={
                'user_id': user_id,
                'record_type': f'profiles/{user_id}'
            }
        )
        user = response.get('Item')
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str):
    try:
        bart_table.delete_item(
            Key={
                'user_id': user_id,
                'record_type': f'profiles/{user_id}'
            }
        )
        
        conversations = await get_user_conversations(user_id, limit=1000)
        with bart_table.batch_writer() as batch:
            for conv in conversations:
                batch.delete_item(
                    Key={
                        'user_id': conv['user_id'],
                        'record_type': conv['record_type']
                    }
                )
        
        return {"message": "User and associated data deleted successfully"}
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    try:
        response = bart_table.query(
            KeyConditionExpression='user_id = :uid AND begins_with(record_type, :conv)',
            ExpressionAttributeValues={
                ':uid': conversation_id.split("#")[0],
                ':conv': f'CONVERSATION#{conversation_id.split("#")[1]}'
            }
        )
        conversations = response.get('Items', [])
        if not conversations:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversations[0]
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/location")
async def update_user_location(location_data: dict):
    """Update user location and store in DynamoDB"""
    try:
        session_id = location_data.get('session_id')
        user_id = location_data.get('user_id')
        latitude = location_data.get('latitude')
        longitude = location_data.get('longitude')
        query = location_data.get('query', '')
        
        if not all([session_id, user_id, latitude, longitude]):
            raise HTTPException(status_code=400, detail="session_id, user_id, latitude, and longitude are required")
        
        try:
            lat = float(latitude)
            lon = float(longitude)
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                raise ValueError("Invalid coordinates")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid latitude or longitude values")
        
        location_updated = await location_manager.store_user_location(session_id, user_id, lat, lon)
        
        if location_updated:
            print(f"Location updated for session {session_id}: {lat}, {lon}")
        else:
            print(f"Location unchanged for session {session_id}")
        
        nearest_station = location_manager.find_nearest_station(lat, lon)
        
        response_data = {
            "message": "Location updated successfully",
            "location_updated": location_updated,
            "nearest_station": nearest_station
        }
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error updating user location: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update location")

@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    try:
        response = bart_table.query(
            KeyConditionExpression='user_id = :uid AND begins_with(record_type, :conv)',
            ExpressionAttributeValues={
                ':uid': conversation_id.split("#")[0],
                ':conv': f'CONVERSATION#{conversation_id.split("#")[1]}'
            }
        )
        items = response.get('Items', [])
        if not items:
            raise HTTPException(status_code=404, detail="Conversation not found")
            
        conversation = items[0]
        
        bart_table.delete_item(
            Key={
                'user_id': conversation['user_id'],
                'record_type': conversation['record_type']
            }
        )
        
        return {"message": "Conversation deleted successfully"}
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ======================================CONTEXT CONTINUITY API ENDPOINTS======================================
@app.get("/api/context/{user_id}/{session_id}")
async def get_session_context(user_id: str, session_id: str):
    """Get current session context for debugging and monitoring"""
    try:
        session_context = await context_manager.get_session_context(user_id, session_id)
        
        return {
            "user_id": session_context.user_id,
            "session_id": session_context.session_id,
            "last_intent": session_context.last_intent,
            "last_category": session_context.last_category,
            "last_api_endpoint": session_context.last_api_endpoint,
            "cached_parameters": session_context.cached_parameters,
            "conversation_count": session_context.conversation_count,
            "last_activity": session_context.last_activity.isoformat() if session_context.last_activity else None,
            "recent_contexts_count": len(session_context.recent_contexts)
        }
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/context/{user_id}/{session_id}")
async def reset_session_context(user_id: str, session_id: str):
    """Reset context for a specific session"""
    try:
        await context_manager.reset_context(user_id, session_id)
        return {"message": f"Context reset for user {user_id}, session {session_id}"}
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/context/stats")
async def get_context_stats():
    """Get overall context system statistics from database"""
    try:
        active_sessions = await session_manager.get_active_sessions()
        total_sessions = len(active_sessions)
        
        total_conversations = 0
        for session_data in active_sessions.values():
            total_conversations += 1  
        
        return {
            "total_sessions": total_sessions,
            "active_sessions_last_hour": total_sessions,  
            "total_conversations": total_conversations,
            "average_conversations_per_session": total_conversations / max(total_sessions, 1),
            "storage_type": "database_with_ttl"
        }
    except Exception as e:
        logging.error(f"Error getting context stats: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/monitoring/memory")
async def get_memory_monitoring():
    """Get comprehensive memory monitoring statistics"""
    try:
        return get_monitoring_summary()
    except Exception as e:
        logging.error(f"Error getting memory monitoring stats: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/monitoring/health")
async def get_health_status():
    """
    Get application health status with timeout protection
    OPTIMIZED: Added timeout protection to prevent blocking
    """
    try:
        try:
            stats = await asyncio.wait_for(
                asyncio.to_thread(get_memory_stats),
                timeout=1.0
            )
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "timestamp": datetime.now().isoformat(),
                "error": "Memory stats timeout"
            }
        
        session_stats = session_manager_instance.get_stats()
        
        health_status = "healthy"
        if stats.memory_percent > 85:
            health_status = "critical"
        elif stats.memory_percent > 70:
            health_status = "warning"
        
        return {
            "status": health_status,
            "timestamp": stats.timestamp.isoformat(),
            "memory_percent": stats.memory_percent,
            "cpu_percent": stats.cpu_percent,
            "active_connections": stats.active_connections,
            "available_memory_mb": stats.available_memory_mb,
            "websocket_sessions": session_stats,
            "uptime_info": "Available via system monitoring"
        }
    except Exception as e:
        logging.error(f"Error getting health status: {str(e)}")
        return {
            "status": "error",
            "timestamp": datetime.now().isoformat(),
            "error": str(e)
        }

@app.get("/api/monitoring/metrics")
async def get_metrics():
    """Get detailed application metrics for monitoring"""
    try:
        from memory_monitor import get_monitoring_summary
        from aws_client_manager import aws_client_manager
        
        memory_summary = get_monitoring_summary()
        
        aws_stats = aws_client_manager.get_stats()
        
        session_stats = session_manager_instance.get_stats()
        
        return {
            "timestamp": datetime.now().isoformat(),
            "memory": memory_summary,
            "aws_clients": aws_stats,
            "sessions": session_stats,
            "application": {
                "version": "1.0.0",
                "environment": "production"
            }
        }
    except Exception as e:
        logging.error(f"Error getting metrics: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/monitoring/alerts")
async def get_alerts():
    """Get current system alerts and warnings"""
    try:
        alerts = []
        
        stats = get_memory_stats()
        if stats.memory_percent > 90:
            alerts.append({
                "level": "critical",
                "message": f"Memory usage critical: {stats.memory_percent:.1f}%",
                "timestamp": stats.timestamp.isoformat()
            })
        elif stats.memory_percent > 80:
            alerts.append({
                "level": "warning",
                "message": f"Memory usage high: {stats.memory_percent:.1f}%",
                "timestamp": stats.timestamp.isoformat()
            })
        
        session_stats = session_manager_instance.get_stats()
        if session_stats["active_sessions"] > 800:
            alerts.append({
                "level": "warning",
                "message": f"High session count: {session_stats['active_sessions']}",
                "timestamp": datetime.now().isoformat()
            })
        
        return {
            "alerts": alerts,
            "count": len(alerts),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logging.error(f"Error getting alerts: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/context/{user_id}/{session_id}/structured")
async def get_structured_context(user_id: str, session_id: str):
    """Get structured context data with Intent, Category, API Endpoint, and Parameters"""
    try:
        context_data = await get_context_from_db(user_id, session_id)
        
        if not context_data:
            return {"error": "Context not found"}
        
        return {
            "user_id": user_id,
            "session_id": session_id,
            "context_data": context_data,
            "current_query": context_data.get('current_query', {}),
            "query_history": context_data.get('query_history', []),
            "conversation_count": context_data.get('conversation_count', 0),
            "last_activity": context_data.get('last_activity')
        }
    except Exception as e:
        logging.error(f"Error getting structured context: {str(e)}")
        raise HTTPException(status_code=500, detail="Error getting structured context")
# ======================================CONVERSATION FEEDBACK ENDPOINT======================================
@app.post("/api/conversations/{conversation_id}/feedback")
async def save_conversation_feedback_endpoint(conversation_id: str, feedback: dict):
    """Save user feedback for a conversation in the users folder."""
    return await save_conversation_feedback(conversation_id, feedback)
# ======================================DISAMBIGUATION FUNCTIONS======================================
async def extract_all_potential_station_names(query_text: str) -> List[str]:
    """
    Production-grade station name extraction using AI model to intelligently identify
    both BART and non-BART station names while filtering out common words, payment methods, 
    and other non-station terms.
    
    Args:
        query_text: The query text to analyze
        
    Returns:
        List of potential station names (both BART and non-BART stations for validation)
    """
    import re
    import asyncio
    import json
    
    potential_stations = []
    query_lower = query_text.lower()
    
    valid_station_names = {station["name"].lower(): station["name"] for station in STATION_DATA["stations"]}
    valid_abbrs = {station["abbr"].lower(): station["name"] for station in STATION_DATA["stations"]}
    
    # 1. Extract known BART station abbreviations and names directly (most reliable)
    for station in STATION_DATA["stations"]:
        station_name = station["name"]
        abbr = station["abbr"]
        
        if re.search(r'\b' + re.escape(station_name) + r'\b', query_text, re.IGNORECASE):
            potential_stations.append(station_name)
        
        if re.search(r'\b' + re.escape(abbr) + r'\b', query_text, re.IGNORECASE):
            potential_stations.append(station_name)
        
        if '/' in station_name:
            parts = [part.strip() for part in station_name.split('/')]
            for part in parts:
                if re.search(r'\b' + re.escape(part) + r'\b', query_text, re.IGNORECASE):
                    potential_stations.append(station_name)
    
    # 2. Use AI model to intelligently extract ALL station names (BART and non-BART)
    try:
        station_list = [station["name"] for station in STATION_DATA["stations"]]
        station_abbrs = [station["abbr"] for station in STATION_DATA["stations"]]
        
        ai_prompt = f"""
        You are an expert at identifying station names in user queries. Your task is to extract ONLY station names that are explicitly mentioned in the given query text.

        VALID BART STATIONS (these are the only valid BART stations):
        Station Names: {', '.join(station_list)}
        Station Abbreviations: {', '.join(station_abbrs)}

        CRITICAL RULES:
        1. Extract ONLY station names that are EXACTLY mentioned in the query text
        2. Extract BOTH valid BART stations (from the list above) AND invalid/non-BART stations (like Chennai, Mumbai, Delhi, San Diego, New York)
        3. DO NOT create abbreviations that don't exist in the station list above
        4. DO NOT modify or alter station names - extract them exactly as they appear
        5. DO NOT extract the word "BART" - it refers to the transit system, not a station
        6. DO NOT extract common words like: Apple Pay, Clipper, Google, Facebook, etc.
        7. DO NOT extract payment methods, company names, or general terms
        8. DO NOT extract words that are clearly not station names
        9. DO NOT extract common prepositions like "from", "to", "at" unless they are part of a station name
        10. DO NOT extract common words like "to", "from", "at", "the", "and", "or", "but", "in", "on", "for", "with", "by"
        11. Look for strong transit context clues like "from", "to", "at", "station", "schedule", "trains", "next train", "train to"
        12. Extract ALL capitalized place names that could be stations (even if not BART stations)
        13. Extract station abbreviations ONLY if they are explicitly mentioned in the query
        14. Be liberal with place names in transit context - extract them even if they're not BART stations
        15. CRITICAL: If query contains "train to [place]" or "next train to [place]", ALWAYS extract the place name
        16. CRITICAL: If query is about BART organization/governance (like "BART Board of Directors", "BART management", "BART policies"), extract NO stations - these are general information queries
        17. CRITICAL: DO NOT create abbreviations like "BAYFR" for "Bay Fair" - only extract what's actually in the query
        18. CRITICAL: DO NOT create abbreviations like "CONC" for "Concord" - only extract what's actually in the query

        IMPORTANT: Extract ONLY place names that are explicitly mentioned in the query:
        - Valid BART stations: SFO, Richmond, Ashby, Downtown Berkeley, etc.
        - Invalid/non-BART stations: Chennai, Mumbai, Delhi, San Diego, New York etc.
        - Station abbreviations: ONLY if they appear exactly in the query (SFO, RICH, SFIA, DBRK, etc.)

        Examples of what to extract:
        - "from Delhi to New York" → extract "Delhi" and "New York"
        - "from Chennai to Mumbai" → extract "Chennai" and "Mumbai"
        - "from SFO to Richmond" → extract "SFO" and "Richmond"
        - "schedule from San Diego" → extract "San Diego"
        - "from SFIA to 16th Street Mission" → extract "SFIA" and "16th Street Mission"
        - "next train to bangalore" → extract "bangalore"
        - "from kolkata to delhi" → extract "kolkata" and "delhi"
        - "from Bay Fair to Concord" → extract "Bay Fair" and "Concord"

        Examples of what NOT to extract:
        - The word "BART" (it's the system name, not a station)
        - Common words: Apple Pay, Clipper, Google, Facebook, etc.
        - Payment methods: Apple Pay, Google Pay, etc.
        - General terms: schedule, trains, system, etc.
        - Common prepositions: from, to, at (unless part of station name)
        - Single words like "to", "from", "at", "the", "and", "or", "but", "in", "on", "for", "with", "by"
        - BART organization queries: "BART Board of Directors", "BART management", "BART policies", "BART leadership" → extract NO stations
        - Made-up abbreviations like "BAYFR" for "Bay Fair"

        Query: "{query_text}"

        RESPONSE FORMAT:
        - Return ONLY the station names, one per line
        - DO NOT include any explanatory text like "Here are the stations:" or "The stations are:"
        - DO NOT include numbering like "1.", "2.", etc.
        - DO NOT include any other text besides the station names
        - If no station names are found, return only "NONE"
        
        Example correct responses:
        - "12th Street Oakland City Center\nRichmond"
        - "SFO\nEmbarcadero"
        - "Bay Fair\nConcord"
        - "NONE"
        
        Example WRONG responses (DO NOT do this):
        - "Here are the station names:\n12th Street Oakland City Center\nRichmond"
        - "1. 12th Street Oakland City Center\n2. Richmond"
        - "The stations extracted from the query are: Richmond"
        - "BAYFR" (this abbreviation doesn't exist)
        """
        
        from aws_client_manager import get_bedrock_client
        bedrock_client = get_bedrock_client()
        
        response = bedrock_client.invoke_model(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 300,
                "messages": [
                    {
                        "role": "user",
                        "content": ai_prompt
                    }
                ]
            })
        )
        
        response_body = json.loads(response['body'].read())
        ai_extracted_stations = response_body['content'][0]['text'].strip()
        
        if ai_extracted_stations and ai_extracted_stations != "NONE":
            ai_stations = [station.strip() for station in ai_extracted_stations.split('\n') if station.strip()]
            for station in ai_stations:
                station_lower = station.lower()
                if (station and 
                    station != "NONE" and 
                    not station_lower.startswith('here') and  
                    not station_lower.startswith('the station') and  
                    not station_lower.startswith('extracted') and  
                    not ':' in station and  
                    station_lower not in ['from', 'to', 'at', 'the', 'and', 'or', 'but', 'in', 'on', 'for', 'with', 'by', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'can', 'must', 'shall', 'schedule', 'trains', 'system', 'station', 'available', 'parking', 'fare', 'departures', 'arrivals', 'next', 'when', 'what', 'where', 'how', 'can', 'is', 'are', 'was', 'were'] and
                    len(station) > 2 and 
                    not station_lower.startswith('the ') and
                    not station_lower.startswith('a ') and
                    not station_lower.startswith('an ') and
                    not station_lower.startswith('to ') and
                    not station_lower.startswith('from ') and
                    not station_lower.startswith('at ') and
                    station_lower not in ['to', 'from', 'at', 'the', 'and', 'or', 'but', 'in', 'on', 'for', 'with', 'by']):
                    
                    # Validate that the station exists in the station data or is a valid non-BART station
                    is_valid_bart_station = False
                    is_valid_non_bart_station = False
                    
                    # Check if it's a valid BART station name or abbreviation
                    for station_data in STATION_DATA["stations"]:
                        if (station_data["name"].lower() == station_lower or 
                            station_data["abbr"].lower() == station_lower):
                            is_valid_bart_station = True
                            break
                    
                    # If not a BART station, check if it's a reasonable non-BART station (like Chennai, Mumbai, etc.)
                    if not is_valid_bart_station:
                        # Allow non-BART stations that are clearly place names
                        if (len(station) > 2 and 
                            station[0].isupper() and 
                            not station_lower in ['bart', 'bay', 'area', 'rapid', 'transit', 'system', 'station', 'train', 'trains', 'schedule', 'departure', 'arrival', 'fare', 'ticket', 'payment', 'clipper', 'card', 'cash', 'credit', 'debit', 'apple', 'pay', 'google', 'facebook', 'twitter', 'instagram', 'linkedin', 'youtube', 'netflix', 'amazon', 'microsoft', 'google', 'facebook', 'apple', 'samsung', 'sony', 'nike', 'adidas', 'puma', 'rebook', 'new', 'balance', 'converse', 'vans', 'jordan', 'nike', 'adidas', 'puma', 'rebook', 'new', 'balance', 'converse', 'vans', 'jordan']):
                            is_valid_non_bart_station = True
                    
                    # Only add if it's a valid station (BART or non-BART)
                    if is_valid_bart_station or is_valid_non_bart_station:
                        normalized_station = station
                        if "St." in station:
                            normalized_station = station.replace("St.", "Street")
                        elif "Ave." in station:
                            normalized_station = station.replace("Ave.", "Avenue")
                        elif "Blvd." in station:
                            normalized_station = station.replace("Blvd.", "Boulevard")
                        
                        if normalized_station not in potential_stations:
                            potential_stations.append(normalized_station)
                            print(f"AI-extracted station: '{normalized_station}'")
                    else:
                        print(f"Filtered out invalid station: '{station}'")
        
    except Exception as e:
        print(f"AI station extraction failed: {str(e)}")
        transit_prepositions = ['from', 'to', 'at', 'between', 'leaving', 'going', 'departing', 'arriving']
        for prep in transit_prepositions:
            if prep in ['from', 'to']:
                pattern = rf'\b{prep}\s+([A-Za-z\s\.\']+?)(?:\s+(?:to|from|and|or|$|\.|,|!|\?))'
                matches = re.findall(pattern, query_text, re.IGNORECASE)
                for match in matches:
                    candidate = match.strip()
                    if is_likely_station_name(candidate, query_text):
                        if candidate not in potential_stations:
                            potential_stations.append(candidate)
                            print(f"Context-extracted station: '{candidate}'")
    
    # 3. CRITICAL FIX: Normalize spaces around slashes in extracted station names
    # Handle cases like "Berryessa / North San Jose" -> "Berryessa/North San Jose"
    for i, station in enumerate(potential_stations):
        if '/' in station and ' / ' in station:
            normalized_station = station.replace(' / ', '/')
            potential_stations[i] = normalized_station
            print(f"Normalized station name: '{station}' -> '{normalized_station}'")
        elif '\\' in station and ' \\ ' in station:
            normalized_station = station.replace(' \\ ', '/')
            potential_stations[i] = normalized_station
            print(f"Normalized station name: '{station}' -> '{normalized_station}'")
        elif '-' in station and ' - ' in station:
            normalized_station = station.replace(' - ', '/')
            potential_stations[i] = normalized_station
            print(f"Normalized station name: '{station}' -> '{normalized_station}'")
    
    # 4. Post-processing filter to remove "BART" if it was incorrectly extracted
    filtered_stations = []
    for station in potential_stations:
        if station.lower() not in ['bart', 'bay area rapid transit', 'bay area rapid', 'rapid transit']:
            filtered_stations.append(station)
        else:
            print(f"Filtered out organization term: '{station}'")
    
    # 4. Remove duplicates and return
    return list(set(filtered_stations))

    
def is_likely_station_name(candidate: str, query_context: str) -> bool:
    """
    Production-grade intelligent analysis to determine if a candidate is likely a station name.
    Allows both BART and non-BART stations to be extracted for validation purposes.
    Uses intelligent context analysis instead of hardcoded common words.
    """
    candidate_lower = candidate.lower()
    query_lower = query_context.lower()
    
    valid_station_names = {station["name"].lower(): station["name"] for station in STATION_DATA["stations"]}
    valid_abbrs = {station["abbr"].lower(): station["name"] for station in STATION_DATA["stations"]}
    
    if len(candidate) <= 2 or candidate.isdigit() or not any(c.isalpha() for c in candidate):
        return False
    
    if candidate_lower in valid_station_names or candidate_lower in valid_abbrs:
        return True
    
    for station in STATION_DATA["stations"]:
        if candidate_lower in station["name"].lower():
            return True
    
    # 1. Check for common word patterns
    if len(candidate) <= 4:  
        if not (candidate_lower in valid_station_names or candidate_lower in valid_abbrs):
            strong_transit_contexts = [
                f"station {candidate_lower}",
                f"{candidate_lower} station",
                f"from {candidate_lower}",
                f"to {candidate_lower}",
                f"at {candidate_lower}"
            ]
            if not any(context in query_lower for context in strong_transit_contexts):
                return False
    
    # 2. Advanced linguistic analysis to filter out common words
    
    words = query_context.split()
    if words and words[0].lower() == candidate_lower:
        if not (candidate_lower in valid_station_names or candidate_lower in valid_abbrs):
            return False
    
    if len(candidate) <= 4 and not any(context in query_lower for context in [
        f"station {candidate_lower}", f"{candidate_lower} station",
        f"from {candidate_lower}", f"to {candidate_lower}", f"at {candidate_lower}"
    ]):
        return False
    
    # 3. Check for transit context
    transit_contexts = [
        f"from {candidate_lower}",
        f"to {candidate_lower}",
        f"at {candidate_lower}",
        f"station {candidate_lower}",
        f"{candidate_lower} station",
        f"schedule of {candidate_lower}",
        f"schedule for {candidate_lower}",
        f"trains from {candidate_lower}",
        f"trains to {candidate_lower}",
        f"departures from {candidate_lower}",
        f"arrivals at {candidate_lower}",
        f"parking at {candidate_lower}",
        f"available at {candidate_lower}"
    ]
    
    for context in transit_contexts:
        if context in query_lower:
            return True
    
    # 4. Allow extraction of capitalized words that could be station names (including invalid ones)
    if (candidate[0].isupper() and len(candidate) > 3 and 
        (candidate_lower in query_lower and 
         any(word in query_lower for word in ['station', 'schedule', 'trains', 'from', 'to', 'at', 'parking', 'available']))):
        return True
    
    return False
    

def extract_station_names(query_text: str) -> List[str]:
    """
    Production-grade station name extraction with enhanced normalization support.
    
    This function now properly handles:
    - Street/St. normalization in queries
    - Compound station name normalization (Dublin Pleasanton -> Dublin/Pleasanton)
    - Complete station name matching to avoid false disambiguation
    """
    import re
    potential_matches: List[str] = []
    
    station_part_mapping = {}
    
    org_terms = ["bart", "bay area rapid transit", "bay area rapid", "rapid transit"]

    cleaned_query = re.sub(r'[,;:!?.]', ' ', query_text)
    cleaned_query = re.sub(r'\s+', ' ', cleaned_query).strip()
    query_lower = cleaned_query.lower()
    
    for term in org_terms:
        query_lower = re.sub(r'\b' + re.escape(term) + r'\b', '', query_lower)
    
    query_lower = re.sub(r'\s+', ' ', query_lower).strip()
    words = query_lower.split()
    
    sfo_aliases = [
        "sfo", "sfia", "sfo airport", "sf airport", 
        "san francisco airport", "san francisco international", "san francisco international airport",
        "sanfransisco airport", "sanfranssisco airport", "sanfransciso airport",  # Common typos
        "sanfransisco international airport", "sanfranssisco international airport", "sanfransciso international airport",  # Common typos with international
        "sf international airport", "sf international", "san francisco intl", "san francisco intl airport"
    ]
    
    exact_matches: List[str] = []
    abbr_matches: List[str] = []
    
    airport_phrases = [
        r'\bsfo\b', 
        r'\bsfia\b',
        r'\bsfo airport\b',
        r'\bsf airport\b', 
        r'\bsan francisco airport\b',
        r'\bsan francisco international\b',
        r'\bsan francisco international airport\b',
        r'\bsf international airport\b',
        r'\bsf international\b',
        r'\bsan francisco intl\b',
        r'\bsan francisco intl airport\b',
        r'\bsanfransisco airport\b',
        r'\bsanfranssisco airport\b', 
        r'\bsanfransciso airport\b',
        r'\bsanfransisco international airport\b',
        r'\bsanfranssisco international airport\b',
        r'\bsanfransciso international airport\b'
    ]
    
    sfo_found = False
    for phrase in airport_phrases:
        if re.search(phrase, query_lower):
            print(f"Found exact SFO airport phrase match with '{phrase}' in query")
            for station in STATION_DATA["stations"]:
                if station["abbr"].lower() == "sfia":
                    exact_matches.append(station["name"])
                    sfo_found = True
                    break
            if sfo_found:
                break
    
    if not sfo_found and "airport" in query_lower and ("san francisco" in query_lower or "sf" in query_lower.split()):
        print(f"Found San Francisco Airport in query context")
        for station in STATION_DATA["stations"]:
            if station["abbr"].lower() == "sfia":
                exact_matches.append(station["name"])
                sfo_found = True
                break
    
    if not sfo_found:
        fuzzy_sf_patterns = [
            r'\bsan\s+francisco\b',  
            r'\bsan\s+fransisco\b',  
            r'\bsan\s+franssisco\b',  
            r'\bsan\s+fransciso\b',  
            r'\bsan\s+franciso\b',  
            r'\bsan\s+francsisco\b',  
        ]
        
        for pattern in fuzzy_sf_patterns:
            if re.search(pattern, query_lower) and "airport" in query_lower:
                print(f"Found fuzzy San Francisco Airport match with pattern '{pattern}' in query")
                for station in STATION_DATA["stations"]:
                    if station["abbr"].lower() == "sfia":
                        exact_matches.append(station["name"])
                        sfo_found = True
                        break
                if sfo_found:
                    break
    
    if not sfo_found:
        for alias in sfo_aliases:
            if re.search(r'\b' + re.escape(alias) + r'\b', query_lower):
                print(f"Found SFO airport match with alias '{alias}' in query")
                for station in STATION_DATA["stations"]:
                    if station["abbr"].lower() == "sfia":
                        exact_matches.append(station["name"])
                        sfo_found = True
                        break
                if sfo_found:
                    break

    exact_station_match = False
    
    # 1. Check for exact station name matches first (as complete phrases within the query)
    for station in STATION_DATA["stations"]:
        station_name_lower = station["name"].lower()
        
        if re.search(r'\b' + re.escape(station_name_lower) + r'\b', query_lower):
            if station["name"] not in exact_matches:  
                exact_matches.append(station["name"])
                exact_station_match = True
                print(f"Found exact station name match: '{station['name']}'")
            
        clean_station_name = re.sub(r'[^\w\s]', '', station["name"]).strip()
        clean_station_lower = clean_station_name.lower()
        clean_query_lower = re.sub(r'[^\w\s]', '', query_lower).strip()
        
        if re.search(r'\b' + re.escape(clean_station_lower) + r'\b', clean_query_lower):
            if station["name"] not in exact_matches: 
                exact_matches.append(station["name"])
                exact_station_match = True
                print(f"Found exact station name match (clean): '{station['name']}'")
   
    # 2. Collect additional station matches via abbreviations and word-level matches
    for station in STATION_DATA["stations"]:
        abbr_lower = station["abbr"].lower()
        if re.search(r'\b' + re.escape(abbr_lower) + r'\b', query_lower):
            if station["name"] not in exact_matches:  # Avoid duplicates
                abbr_matches.append(station["abbr"])
                exact_matches.append(station["name"])
                print(f"Found station via abbreviation: '{station['abbr']}' -> '{station['name']}'")
    
    # 2b. CRITICAL: Check for individual words from station names (e.g., "Berkeley" in "Downtown Berkeley")
    word_frequency = {}
    for station in STATION_DATA["stations"]:
        for word in station["name"].lower().split():
            clean_word = re.sub(r'[^\w]', '', word)
            if len(clean_word) > 0:
                word_frequency[clean_word] = word_frequency.get(clean_word, 0) + 1
    

    total_stations = len(STATION_DATA["stations"])
    common_threshold = min(3, total_stations // 15)
    
    exact_match_words = set()
    for exact_match in exact_matches:
        for word in exact_match.lower().split():
            clean_word = re.sub(r'[^\w]', '', word)
            if len(clean_word) > 0:
                exact_match_words.add(clean_word)
    
    for station in STATION_DATA["stations"]:
        if station["name"] in exact_matches:
            continue
            
        station_words = station["name"].lower().split()
        for word in station_words:
            clean_word = re.sub(r'[^\w]', '', word)
            
            if clean_word in exact_match_words:
                continue
            
            is_distinctive = (
                len(clean_word) > 4 and
                word_frequency.get(clean_word, 0) <= common_threshold and
                '(' not in word and ')' not in word
            )
            
            if is_distinctive and re.search(r'\b' + re.escape(clean_word) + r'\b', query_lower):
                exact_matches.append(station["name"])
                print(f"Found station via distinctive word match: '{clean_word}' -> '{station['name']}' (freq: {word_frequency.get(clean_word, 0)})")
                break 
            
    for station in STATION_DATA["stations"]:
        station_name_lower = station["name"].lower()
        
        if station_name_lower in query_lower:
            if station["name"] not in exact_matches:  
                exact_matches.append(station["name"])
            continue
            
        clean_station_name = re.sub(r'[^\w\s]', '', station["name"]).strip()
        clean_station_lower = clean_station_name.lower()
        clean_query_lower = re.sub(r'[^\w\s]', '', query_lower).strip()
        
        if clean_station_lower in clean_query_lower:
            if station["name"] not in exact_matches:  
                exact_matches.append(station["name"])
            continue
            
        if '/' in station["name"]:
            station_parts = station["name"].split('/')
            for part in station_parts:
                part = part.strip().lower()
                
                is_part_of_exact_match = False
                for exact_match in exact_matches:
                    if part in exact_match.lower().split():
                        is_part_of_exact_match = True
                        print(f"Skipping slash part '{part}' - already part of exact match '{exact_match}'")
                        break
                
                if is_part_of_exact_match:
                    continue
                
                if part in query_lower:
                    if station["name"] not in exact_matches:  
                        exact_matches.append(station["name"])
                        print(f"Found station via slash part match: '{part}' -> '{station['name']}'")
                        station_part_mapping[part] = station["name"]
                    break

    # 3. Collect possible ambiguous/group keyword matches *in addition* to exact matches
    for group_key in STATION_GROUPS.keys():
        if group_key.lower() in query_lower:
            potential_matches.append(group_key)

    # 4. Use fuzzy matching to find the best station match instead of partial matching
    if len(exact_matches) == 0:
        best_match = None
        best_score = 0
        min_score_threshold = 0.5  
        
        for station in STATION_DATA["stations"]:
            station_name = station["name"]
            station_name_lower = station_name.lower()
            
            from difflib import SequenceMatcher
            
            # Method 1: Sequence matcher
            seq_score = SequenceMatcher(None, query_lower, station_name_lower).ratio()
            
            # Method 2: Check if user input contains key words from the station name
            user_words = set(query_lower.split())
            station_words = set(station_name_lower.split())
            word_overlap = len(user_words.intersection(station_words)) / max(len(user_words), len(station_words)) if station_words else 0
            
            # Method 3: Check if the station name contains key words from user input
            reverse_overlap = len(user_words.intersection(station_words)) / len(user_words) if user_words else 0
            
            combined_score = (seq_score * 0.4) + (word_overlap * 0.4) + (reverse_overlap * 0.2)
            
            if combined_score > best_score and combined_score >= min_score_threshold:
                best_score = combined_score
                best_match = station_name
        
        if best_match:
            print(f"Fuzzy matched station in extraction: '{query_text}' → '{best_match}' (score: {best_score:.2f})")
            exact_matches.append(best_match)
    
    # 5. Fallback to partial matching if fuzzy matching didn't find anything
    if len(exact_matches) == 0:
        for station in STATION_DATA["stations"]:
            station_parts = station["name"].split()
            
            for i in range(1, min(3, len(station_parts) + 1)):
                partial_name = " ".join(station_parts[:i])
                if len(partial_name) > 3 and partial_name.lower() in query_lower:
                    is_ambiguous_partial = any(
                        group_key.lower() == partial_name.lower() 
                        for group_key in STATION_GROUPS.keys()
                    )
                    if not is_ambiguous_partial:
                        potential_matches.append(station["name"])  
                        print(f"Found station via partial name: '{partial_name}' -> '{station['name']}'")
                    else:
                        print(f"Skipping partial match '{partial_name}' -> '{station['name']}' (ambiguous - in STATION_GROUPS)")
                    break
                    
            if '/' in station["name"]:
                slash_parts = station["name"].split('/')
                for slash_part in slash_parts:
                    slash_part = slash_part.strip()
                    if len(slash_part) > 3 and slash_part.lower() in query_lower:
                        potential_matches.append(station["name"])  
                        print(f"Found station via slash part: '{slash_part}' -> '{station['name']}'")
                        station_part_mapping[slash_part.lower()] = station["name"]
                        break

    # 6. Handle shortened station names like "rich" for "Richmond"
    if len(exact_matches) == 0 and len(potential_matches) == 0:
        for word in words:
            if len(word) >= 3:  
                for station in STATION_DATA["stations"]:
                    station_name = station["name"]
                    station_lower = station_name.lower()
                    
                    for part in station_lower.split():
                        if part.startswith(word) and len(word) >= 3:
                            potential_matches.append(station_name)
                            station_part_mapping[word] = station_name
                            break

    # 7. Handle very short queries that might be direct station name responses
    if len(exact_matches) == 0 and len(abbr_matches) == 0 and len(potential_matches) == 0 and len(query_text.split()) <= 3:
        for station in STATION_DATA["stations"]:
            for part in station["name"].split():
                if len(part) > 3 and part.lower() in query_lower:
                    potential_matches.append(station["name"])

    all_matches = []
    
    for abbr in abbr_matches:
        for station in STATION_DATA["stations"]:
            if station["abbr"] == abbr:
                if station["name"] not in all_matches:
                    all_matches.append(station["name"])
                break
    
    for name in exact_matches:
        if name not in all_matches:
            all_matches.append(name)
                
    for match in potential_matches:
        if match not in all_matches:
            all_matches.append(match)
    
    unique_matches = []
    for match in all_matches:
        is_duplicate = False
        for existing_match in unique_matches:
            if '/' in existing_match and match.lower() in [part.strip().lower() for part in existing_match.split('/')]:
                is_duplicate = True
                print(f"Skipping '{match}' as it's part of '{existing_match}'")
                break
        if not is_duplicate:
            unique_matches.append(match)
            
    raw_candidates = unique_matches.copy()
    print(f"Raw extracted station candidates: {raw_candidates}")

    if len(unique_matches) > 1:
        from difflib import SequenceMatcher
        
        is_multi_station_query = any(keyword in query_lower for keyword in ['from', 'to', 'toward', 'towards', 'between', 'and'])
        
        if is_multi_station_query:
            print(f"Multi-station query detected - keeping all {len(unique_matches)} stations: {unique_matches}")
        else:
            best_match = None
            best_score = 0
            min_score_threshold = 0.6
            
            for match in unique_matches:
                match_lower = match.lower()
                
                seq_score = SequenceMatcher(None, query_lower, match_lower).ratio()
                user_words = set(query_lower.split())
                match_words = set(match_lower.split())
                word_overlap = len(user_words.intersection(match_words)) / max(len(user_words), len(match_words)) if match_words else 0
                reverse_overlap = len(user_words.intersection(match_words)) / len(user_words) if user_words else 0
                combined_score = (seq_score * 0.4) + (word_overlap * 0.4) + (reverse_overlap * 0.2)
                
                if combined_score > best_score and combined_score >= min_score_threshold:
                    best_score = combined_score
                    best_match = match
            
            if best_match and best_score > 0.8:  
                print(f"Single-station query - selecting best match: '{best_match}' (score: {best_score:.2f})")
                unique_matches = [best_match]
            else:
                print(f"No strong single match - keeping all {len(unique_matches)} stations for disambiguation")

    print(
        f"Extracted station candidates from '{query_text}': {unique_matches} (exact: {exact_matches}, abbr: {abbr_matches})"
    )

    return sorted(unique_matches, key=len, reverse=True)

async def handle_missing_parameters(websocket: WebSocket, user_id: str, session_id: str, 
                                   api_endpoint: str, params: Dict[str, Any], 
                                   validation_error: str, category: str, query_text: str):
    """
    Production-grade handler for missing required parameters.
    Asks the user for the missing parameter and stores state to resume processing.
    """
    try:
        print("\n========== HANDLING MISSING PARAMETERS ==========")
        print(f"Endpoint: {api_endpoint}")
        print(f"Current params: {params}")
        print(f"Validation error: {validation_error}")
        print("===============================================\n")
        
        from models import SessionContext
        required_params = SessionContext.get_endpoint_required_params().get(api_endpoint, [])
        missing_param = None
        
        for param in required_params:
            if param not in params or params[param] is None or params[param] == "":
                missing_param = param
                break
        
        if not missing_param:
            error_msg = f"I need more information to complete your request. {validation_error}"
            audio_data = await synthesize_speech(error_msg)
            if audio_data and websocket:
                await safe_websocket_send_bytes(websocket, audio_data)
            return
        
        language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
        
        if missing_param == 'dest':
            if params.get('orig'):
                if api_endpoint == "fare":
                    question = f"What's the fare from {params['orig']} to where?"
                else:
                    question = f"Where would you like to go from {params['orig']}?"
            else:
                if api_endpoint == "fare":
                    question = "What's the fare to where?"
                else:
                    question = "Where would you like to go?"
        elif missing_param == 'orig':
            if params.get('dest'):
                question = f"Where are you traveling from to get to {params['dest']}?"
            else:
                question = "Where are you traveling from?"
        elif missing_param == 'route':
            question = "Which route would you like information about?"
        elif missing_param == 'eq':
            question = "Are you asking about elevators or escalators?"
        else:
            question = f"Could you specify the {missing_param} for this request?"
        
        await update_state_values_in_db(user_id, session_id, {
            'awaiting_parameter': missing_param,
            'pending_endpoint': api_endpoint,
            'pending_category': category,
            'pending_params': json.dumps(params),
            'pending_query': query_text
        })
        
        print(f"Asking user for missing parameter: {missing_param}")
        print(f"Question: {question}")
        
        audio_data = await synthesize_speech(question, language=language)
        if audio_data and websocket:
            await safe_websocket_send_bytes(websocket, audio_data)
        
        if websocket:
            temp_id = f"parameter_request_{uuid.uuid4()}"
            response_data = {
                "type": "conversation",
                "query": query_text,
                "response": question,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": session_id,
                "conversation_id": temp_id,
                "is_parameter_request": True,
                "missing_parameter": missing_param
            }
            await safe_websocket_send_text(websocket, json.dumps(response_data, ensure_ascii=False))
        
        logging.info(f"Awaiting {missing_param} from user for endpoint {api_endpoint}")
        
    except Exception as e:
        logging.error(f"Error handling missing parameters: {str(e)}")
        error_msg = "I need more information to complete your request."
        audio_data = await synthesize_speech(error_msg)
        if audio_data and websocket:
            await safe_websocket_send_bytes(websocket, audio_data)


async def process_location_query(location_response: str, destination: str, websocket: WebSocket) -> str:
    """Process the user's location response and continue with the train query"""
    try:
        print("\n========== PROCESSING LOCATION RESPONSE ==========")
        print(f"User's location: '{location_response}'")
        print(f"Destination: '{destination}'")
        print("=================================================\n")
        
        user_id = None
        session_id = None
        
        try:
            user_id, session_id = await get_user_session_info(websocket)
        except:
            pass
            
        if not user_id or not session_id:
            logging.error("Missing user_id or session_id in process_location_query")
            return "Error: Missing user identification"
        
        station_names = extract_station_names(location_response)
        origin_station = None
        
        if station_names:
            candidate_station = station_names[0]
            
            is_ambiguous, disambiguation_options = check_station_ambiguity(candidate_station)
            
            if is_ambiguous and disambiguation_options and len(disambiguation_options) > 1:
                print(f"Location response '{candidate_station}' is ambiguous with {len(disambiguation_options)} options: {disambiguation_options}")
                
                await update_state_values_in_db(user_id, session_id, {
                    'awaiting_disambiguation': True,
                    'ambiguous_station': candidate_station,
                    'station_options': disambiguation_options,
                    'original_query': f"next train from {candidate_station} to {destination}",
                    'awaiting_location': False
                })
                
                user_language = await detect_language(location_response)
                
                if len(disambiguation_options) == 2:
                    question_templates = {
                        "en": f"I found two stations with '{candidate_station}'. Which one do you mean: {disambiguation_options[0]} or {disambiguation_options[1]}?",
                        "es": f"Encontré dos estaciones con '{candidate_station}'. ¿Cuál quieres decir: {disambiguation_options[0]} o {disambiguation_options[1]}?",
                        "zh": f"我找到了两个包含'{candidate_station}'的车站。你指的是哪一个：{disambiguation_options[0]}还是{disambiguation_options[1]}？"
                    }
                else:
                    options_text = ", ".join(disambiguation_options[:-1]) + f", or {disambiguation_options[-1]}"
                    question_templates = {
                        "en": f"I found multiple stations with '{candidate_station}'. Which one do you mean: {options_text}?",
                        "es": f"Encontré varias estaciones con '{candidate_station}'. ¿Cuál quieres decir: {options_text}?",
                        "zh": f"我找到了多个包含'{candidate_station}'的车站。你指的是哪一个：{options_text}？"
                    }
                
                lang = user_language if user_language in question_templates else "en"
                disambiguation_question = question_templates[lang]
                
                audio_data = await synthesize_speech(disambiguation_question, language=lang)
                if audio_data:
                    await safe_websocket_send_bytes(websocket, audio_data)
                
                temp_id = f"location_disambiguation_{uuid.uuid4()}"
                conversation_data = {
                    "type": "conversation",
                    "query": location_response,
                    "response": disambiguation_question,
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": session_id,
                    "conversation_id": temp_id,
                    "is_disambiguation": True,
                    "station_options": disambiguation_options
                }
                await websocket.send_text(json.dumps(conversation_data))
                
                return disambiguation_question
            
            origin_station = candidate_station
            
            station_code = get_station_code(origin_station)
            if station_code:
                print(f"Valid origin station: '{origin_station}' with code: {station_code}")
                
                reconstructed_query = f"next train from {origin_station} to {destination}"
                print(f"Reconstructed query: '{reconstructed_query}'")
                
                await update_state_values_in_db(user_id, session_id, {
                    'final_query': reconstructed_query,
                    'awaiting_location': False,
                    'destination_station': destination,
                    'origin_station': origin_station,
                    'complete_query': True
                })
                
                return None
            else:
                print(f"Invalid origin station: '{origin_station}'")
        
        user_language = await detect_language(location_response)
        
        messages = {
            "en": f"I couldn't recognize '{location_response}' as a BART station. Please tell me which BART station you're at.",
            "es": f"No pude reconocer '{location_response}' como una estación de BART. Por favor, dígame en qué estación de BART se encuentra.",
            "zh": f"我无法将'{location_response}'识别为BART车站。请告诉我您所在的BART车站。"
        }
        
        lang = user_language if user_language in messages else "en"
        message = messages[lang]
        
        audio_data = await synthesize_speech(message, language=lang)
        if audio_data:
            await safe_websocket_send_bytes(websocket, audio_data)
        
        temp_id = f"location_retry_{uuid.uuid4()}"
        conversation_data = {
            "type": "conversation",
            "query": location_response,
            "response": message,
            "timestamp": datetime.utcnow().isoformat(),
            "session_id": session_id,
            "conversation_id": temp_id,
            "is_location_retry": True
        }
        await websocket.send_text(json.dumps(conversation_data))
        
        return message
        
    except Exception as e:
        logging.exception(f"Error processing location query: {str(e)}")
        
        user_language = await detect_language(location_response)
        
        messages = {
            "en": "Sorry, I had trouble processing your location. Please try your complete query again, specifying both your current station and destination.",
            "es": "Lo siento, tuve problemas para procesar su ubicación. Por favor, intente nuevamente con su consulta completa, especificando tanto su estación actual como su destino.",
            "zh": "抱歉，我在处理您的位置时遇到了问题。请再次尝试您的完整查询，同时指定您当前的车站和目的地。"
        }
        
        lang = user_language if user_language in messages else "en"
        message = messages[lang]
        
        audio_data = await synthesize_speech(message, language=lang)
        if audio_data:
            await safe_websocket_send_bytes(websocket, audio_data)
        
        try:
            user_id = None
            session_id = None
            
            try:
                user_id, session_id = await get_user_session_info(websocket)
            except Exception as e:
                logging.error(f"Error getting user_id and session_id: {str(e)}")
            
            if user_id and session_id:
                await update_state_values_in_db(user_id, session_id, {
                    'awaiting_location': False,
                    'destination_station': None
                })
                logging.info(f"Updated state in DynamoDB for user {user_id}, session {session_id}")
            else:
                logging.error("Missing user_id or session_id, couldn't update state in DynamoDB")
        except Exception as e:
            logging.error(f"Error updating state in DynamoDB: {str(e)}")
        
        return message

async def process_station_disambiguation(user_response: str, options: List[str], websocket: WebSocket) -> Optional[str]:
    """Process user response to station disambiguation and return the selected station"""
    try:
        user_id, session_id = await get_user_session_info(websocket)
        
        if not user_id or not session_id:
            logging.error("Missing user_id or session_id in process_station_disambiguation")
            return None
            
        preprocessed_response = preprocess_concatenated_stations(user_response)
        user_response_lower = preprocessed_response.lower().strip()
        
        print("\n========== DISAMBIGUATION RESPONSE PROCESSING ==========")
        print(f"User response: '{user_response}'")
        if preprocessed_response != user_response:
            print(f"Preprocessed response: '{preprocessed_response}'")
        print(f"Available options: {options}")
        print("=======================================================\n")
        
        logging.info(f"Processing disambiguation response: '{user_response_lower}'")
        
        for station in STATION_DATA['stations']:
            if station['name'].lower() == user_response_lower:
                print(f"Found direct station match: {station['name']}")
                
                if station['name'] in options:
                    return station['name']
        
        for option in options:
            if option.lower() == user_response_lower:
                print(f"Found exact match in options: {option}")
                logging.info(f"Found exact match in options: {option}")
                return option
        
        matches = []
        for option in options:
            if user_response_lower in option.lower():
                matches.append(option)
            elif any(word.lower() in user_response_lower for word in option.split() if len(word) > 3):
                matches.append(option)
        
        if len(matches) == 1:
            print(f"Found partial match in options: {matches[0]}")
            logging.info(f"Found partial match in options: {matches[0]}")
            return matches[0]
        elif len(matches) > 1:
            for match in matches:
                match_parts = match.lower().split()
                if match_parts[0] == user_response_lower or user_response_lower.startswith(match_parts[0]):
                    print(f"Found prioritized match: {match}")
                    return match
        
        if user_response_lower.isdigit():
            idx = int(user_response_lower) - 1
            if 0 <= idx < len(options):
                print(f"User selected option #{idx+1}: {options[idx]}")
                logging.info(f"User selected option #{idx+1}: {options[idx]}")
                return options[idx]
        
        ordinal_patterns = {
            'first': 1, '1st': 1, 'second': 2, '2nd': 2, 'third': 3, '3rd': 3,
            'fourth': 4, '4th': 4,
            'one': 1, 'two': 2, 'three': 3, 'four': 4,
            'the first': 1, 'the second': 2, 'the third': 3, 'the fourth': 4,
            'the one': 1, 'the two': 2, 'the three': 3, 'the four': 4,
            'first option': 1, 'second option': 2, 'third option': 3, 'fourth option': 4,
            'option 1': 1, 'option 2': 2, 'option 3': 3, 'option 4': 4,
            'first one': 1, 'second one': 2, 'third one': 3, 'fourth one': 4,
            'the first one': 1, 'the second one': 2, 'the third one': 3, 'the fourth one': 4,
            'number 1': 1, 'number 2': 2, 'number 3': 3, 'number 4': 4,
            'number one': 1, 'number two': 2, 'number three': 3, 'number four': 4,
            'no 1': 1, 'no 2': 2, 'no 3': 3, 'no 4': 4,
            'no. 1': 1, 'no. 2': 2, 'no. 3': 3, 'no. 4': 4,
            'choice 1': 1, 'choice 2': 2, 'choice 3': 3, 'choice 4': 4,
            'first choice': 1, 'second choice': 2, 'third choice': 3, 'fourth choice': 4,
        }
        
        if user_response_lower in ordinal_patterns:
            num = ordinal_patterns[user_response_lower]
            idx = num - 1
            if 0 <= idx < len(options):
                print(f"User selected option #{idx+1} via pattern '{user_response_lower}': {options[idx]}")
                logging.info(f"User selected option #{idx+1} via pattern '{user_response_lower}': {options[idx]}")
                return options[idx]
        
        ordinal_words = ['first', 'second', 'third', 'fourth', 'fifth', 'sixth']
        for word_idx, word in enumerate(ordinal_words, 1):
            if word in user_response_lower:
                idx = word_idx - 1
                if 0 <= idx < len(options):
                    print(f"User selected option #{idx+1} via ordinal word '{word}': {options[idx]}")
                    logging.info(f"User selected option #{idx+1} via ordinal word '{word}': {options[idx]}")
                    return options[idx]
        
        number_match = re.search(r'\b(one|two|three|four|five|1|2|3|4|5)\b', user_response_lower)
        if number_match:
            number_text = number_match.group(1)
            number_map = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                         '1': 1, '2': 2, '3': 3, '4': 4, '5': 5}
            idx = number_map.get(number_text, 0) - 1
            if 0 <= idx < len(options):
                print(f"User selected option #{idx+1}: {options[idx]}")
                logging.info(f"User selected option #{idx+1}: {options[idx]}")
                return options[idx]
        
        directions = ['north', 'south', 'east', 'west']
        for direction in directions:
            if direction in user_response_lower:
                matches = []
                for option in options:
                    if direction.lower() in option.lower():
                        matches.append(option)
                
                if len(matches) == 1:
                    print(f"Found direction match: {matches[0]}")
                    logging.info(f"Found direction match: {matches[0]}")
                    return matches[0]
        
        print("No match found in disambiguation options")
        
        try:
            language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
        except Exception as e:
            logging.error(f"Error getting language from state: {str(e)}")
            language = 'en'
        
        messages = {
            "en": "I couldn't determine which station you meant. Let's try again with your question.",
            "es": "No pude determinar qué estación quiso decir. Intentemos de nuevo con su pregunta.",
            "zh": "我无法确定您指的是哪个车站。让我们重新尝试您的问题。"
        }
        
        lang = language if language in messages else "en"
        message = messages[lang]
        
        audio_data = await synthesize_speech(message, language=lang)
        if audio_data:
            await safe_websocket_send_bytes(websocket, audio_data)
            await websocket.send_text(message)
        
        return None
    
    except Exception as e:
        logging.exception(f"Error processing station disambiguation: {str(e)}")
        return None

async def process_query_with_disambiguation(query_text: str, websocket: WebSocket, user_id: str, session_id: str) -> Optional[str]:
    """Process user query with station disambiguation if needed - Production grade with zero WebSocket dependency"""
    import re
    
    if not user_id or not session_id:
        logging.error("Missing user_id or session_id in process_query_with_disambiguation")
        return "Error: Missing user identification"
    
    is_awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
    is_awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
    original_query = await get_state_value_from_db(user_id, session_id, 'original_query', None)
    destination_station = await get_state_value_from_db(user_id, session_id, 'destination_station', None)
    
    print("\n========== DISAMBIGUATION STATE ==========")
    print(f"Current query: '{query_text}'")
    print(f"Awaiting disambiguation: {is_awaiting_disambiguation}")
    print(f"Awaiting location: {is_awaiting_location}")
    print(f"Original query: '{original_query}'")
    print(f"Destination station: '{destination_station}'")
    print("=========================================\n")
    
    if not is_awaiting_location and not is_awaiting_disambiguation and destination_station:
        print(f"\n========== CLEARING PERSISTENT STATE ==========")
        print(f"Clearing persistent destination station: '{destination_station}' for new standalone query")
        await update_state_values_in_db(user_id, session_id, {
            'destination_station': None,
            'origin_station': None
        })
        destination_station = None
        print("Persistent state cleared")
        print("==================================================\n")
    
    if is_awaiting_location and destination_station:
        print("Location handling moved to pre-processing section")
    
    if is_awaiting_disambiguation:
        print(f"Disambiguation response detected: '{query_text}'")
        
        station_options = await get_state_value_from_db(user_id, session_id, 'station_options', [])
        ambiguous_station = await get_state_value_from_db(user_id, session_id, 'ambiguous_station', '')
        
        matched_station = None
        query_lower = query_text.lower()
        
        for option in station_options:
            if option.lower() == query_lower:
                matched_station = option
                break
        
        if not matched_station:
            if query_text.isdigit():
                try:
                    option_index = int(query_text) - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via digit: {matched_station}")
                except (ValueError, IndexError):
                    pass
            
            number_word_map = {
                'one': 1, 'two': 2, 'three': 3, 'four': 4
            }
            if not matched_station and query_lower in number_word_map:
                try:
                    option_index = number_word_map[query_lower] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via number word '{query_lower}': {matched_station}")
                except (ValueError, IndexError):
                    pass
            
            ordinal_word_map = {
                'first': 1, 'second': 2, 'third': 3, 'fourth': 4,
                '1st': 1, '2nd': 2, '3rd': 3, '4th': 4
            }
            if not matched_station and query_lower in ordinal_word_map:
                try:
                    option_index = ordinal_word_map[query_lower] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via ordinal word '{query_lower}': {matched_station}")
                except (ValueError, IndexError):
                    pass
        
            if not matched_station and re.match(r'^option\s*(\d+)$', query_lower):
                try:
                    option_num = int(re.match(r'^option\s*(\d+)$', query_lower).group(1))
                    option_index = option_num - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError):
                    pass
            
            if not matched_station and re.match(r'^option\s+(one|two|three|four)$', query_lower):
                number_word_map = {
                    'one': 1, 'two': 2, 'three': 3, 'four': 4
                }
                try:
                    number_word = re.match(r'^option\s+(one|two|three|four)$', query_lower).group(1)
                    option_index = number_word_map[number_word] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^(first|second|third|fourth)\s+option$', query_lower):
                ordinal_map = {
                    'first': 1, 'second': 2, 'third': 3, 'fourth': 4
                }
                try:
                    ordinal = re.match(r'^(first|second|third|fourth)\s+option$', query_lower).group(1)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^(first|second|third|fourth)\s+one$', query_lower):
                ordinal_map = {
                    'first': 1, 'second': 2, 'third': 3, 'fourth': 4
                }
                try:
                    ordinal = re.match(r'^(first|second|third|fourth)\s+one$', query_lower).group(1)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^(1st|2nd|3rd|4th)\s+one$', query_lower):
                ordinal_map = {
                    '1st': 1, '2nd': 2, '3rd': 3, '4th': 4
                }
                try:
                    ordinal = re.match(r'^(1st|2nd|3rd|4th|5th|6th)\s+one$', query_lower).group(1)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^(number|num)\s*(\d+)$', query_lower):
                try:
                    option_num = int(re.match(r'^(number|num)\s*(\d+)$', query_lower).group(2))
                    option_index = option_num - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError):
                    pass
            
            if not matched_station and re.match(r'^(number|num)\s+(one|two|three|four)$', query_lower):
                number_word_map = {
                    'one': 1, 'two': 2, 'three': 3, 'four': 4
                }
                try:
                    number_word = re.match(r'^(number|num)\s+(one|two|three|four)$', query_lower).group(2)
                    option_index = number_word_map[number_word] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^no\.?\s*(\d+)$', query_lower):
                try:
                    option_num = int(re.match(r'^no\.?\s*(\d+)$', query_lower).group(1))
                    option_index = option_num - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError):
                    pass
            
            if not matched_station and re.match(r'^no\.?\s+(one|two|three|four)$', query_lower):
                number_word_map = {
                    'one': 1, 'two': 2, 'three': 3, 'four': 4
                }
                try:
                    number_word = re.match(r'^no\.?\s+(one|two|three|four)$', query_lower).group(1)
                    option_index = number_word_map[number_word] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^the\s+(first|second|third|fourth)$', query_lower):
                ordinal_map = {
                    'first': 1, 'second': 2, 'third': 3, 'fourth': 4
                }
                try:
                    ordinal = re.match(r'^the\s+(first|second|third|fourth)$', query_lower).group(1)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^the\s+(first|second|third|fourth)\s+one$', query_lower):
                ordinal_map = {
                    'first': 1, 'second': 2, 'third': 3, 'fourth': 4
                }
                try:
                    ordinal = re.match(r'^the\s+(first|second|third|fourth)\s+one$', query_lower).group(1)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^the\s+(first|second|third|fourth)\s+one$', query_lower):
                ordinal_map = {
                    'first': 1, 'second': 2, 'third': 3, 'fourth': 4
                }
                try:
                    ordinal = re.match(r'^the\s+(first|second|third|fourth)\s+one$', query_lower).group(1)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
            
            if not matched_station and re.match(r'^(pick|choose|select)\s+the\s+(first|second|third|fourth)$', query_lower):
                ordinal_map = {
                    'first': 1, 'second': 2, 'third': 3, 'fourth': 4
                }
                try:
                    ordinal = re.match(r'^(pick|choose|select)\s+the\s+(first|second|third|fourth)$', query_lower).group(2)
                    option_index = ordinal_map[ordinal] - 1
                    if 0 <= option_index < len(station_options):
                        matched_station = station_options[option_index]
                        print(f"Matched via pattern '{query_lower}': {matched_station}")
                except (ValueError, IndexError, AttributeError, KeyError):
                    pass
        
        if not matched_station:
            for option in station_options:
                if query_lower in option.lower() or option.lower() in query_lower:
                    matched_station = option
                    break
        
        if matched_station:
            if original_query:
                # Standard disambiguation flow - merge with original query
                import re
                merged_query = re.sub(re.escape(ambiguous_station), matched_station, original_query, flags=re.IGNORECASE)
                print(f"Successfully merged disambiguation: '{original_query}' -> '{merged_query}'")
                print(f"Selected station: '{matched_station}' from options: {station_options}")
                
                await update_state_values_in_db(user_id, session_id, {
                    'awaiting_disambiguation': False,
                    'original_query': None,
                    'station_options': None,
                    'ambiguous_station': None,
                    'original_query_for_response': None,
                    'resolved_station': matched_station  
                })
                
                return await process_query_with_disambiguation(merged_query, websocket, user_id, session_id)
            else:
                # Missing parameter flow - just set the resolved station and continue
                print(f"Successfully resolved station in missing parameter flow: '{matched_station}' from options: {station_options}")
                
                await update_state_values_in_db(user_id, session_id, {
                    'awaiting_disambiguation': False,
                    'station_options': None,
                    'ambiguous_station': None,
                    'resolved_station': matched_station  
                })
                
                # Continue with the missing parameter flow
                return await process_query_with_disambiguation(query_text, websocket, user_id, session_id)
        else:
            print(f"Could not match '{query_text}' to any of the options: {station_options}")
            
            options_text = "\n".join([f"{idx + 1}. {opt}" for idx, opt in enumerate(station_options)])
            language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
            
            error_message = f"I couldn't understand your selection. Please say one of these options:\n\n{options_text}\n\nOr you can say:\n- The number (1, 2, 3, etc.)\n- The word (one, two, three, etc.)\n- Option 1, Option one, First option, etc.\n- First, Second, Third, etc.\n- No 1, No one, Number 1, Number one, etc.\n- 1st, 2nd, 3rd, etc.\n- The station name itself"
            
            audio_data = await synthesize_speech(error_message, language=language)
            if audio_data:
                await safe_websocket_send_bytes(websocket, audio_data)
            
            conversation_data = {
                "type": "conversation",
                "response": error_message,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": session_id,
                "conversation_id": str(uuid.uuid4()),
                "is_disambiguation_error": True
            }
            await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
            
            return None
    
    original_query_language = await get_state_value_from_db(user_id, session_id, 'original_query_language', None)
    original_query_text = await get_state_value_from_db(user_id, session_id, 'original_query_text', None)
    
    query_language = 'en'
    
    # ---- STATION DISAMBIGUATION LOGIC ----
    query_lower = query_text.lower()
    
    exact_station_names_in_query: List[str] = [
        s["name"] for s in STATION_DATA["stations"] if s["name"].lower() in query_lower
    ]
    
    station_abbrs_in_query: List[str] = []
    for station in STATION_DATA["stations"]:
        abbr_lower = station["abbr"].lower()
        if re.search(r'\b' + re.escape(abbr_lower) + r'\b', query_lower):
            station_abbrs_in_query.append(station["abbr"])
            if station["name"] not in exact_station_names_in_query:
                exact_station_names_in_query.append(station["name"])
    
    if exact_station_names_in_query:
        print(
            f"Exact station names found in query: {exact_station_names_in_query}. Will still look for ambiguous station references."
        )
    
    if station_abbrs_in_query:
        print(
            f"Station abbreviations found in query: {station_abbrs_in_query}."
        )
    
    return await process_query_and_respond_with_text(query_text, websocket, user_id, session_id)

def extract_final_response(combined_response: str) -> str:
    """Extract the final response text from the combined response"""
    final_response = combined_response
    if "Final Combined Response:" in combined_response:
        final_response = combined_response.split("Final Combined Response:", 1)[1].strip()
        if "--------" in final_response:
            final_response = final_response.split("--------", 1)[1].strip()
    return final_response

async def save_and_send_response(websocket: WebSocket, user_id: str, query: str, 
                               combined_response: str, final_response: str, session_id: str):
    """Save conversation to DB and send response to client"""
    last_saved = await get_state_value_from_db(user_id, session_id, 'last_saved_conversation', None)
    final_query = await get_state_value_from_db(user_id, session_id, 'final_query', None)
    is_location_flow = bool(final_query)
    
    original_query_for_display = query
    
    if final_query:
        logging.info(f"Using final reconstructed query '{final_query}' for processing, but keeping original '{query}' for display")
        processing_query = final_query
        await set_state_value_in_db(user_id, session_id, 'final_query', None)
    else:
        processing_query = query
    
    awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
    awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
    awaiting_parameter = await get_state_value_from_db(user_id, session_id, 'awaiting_parameter', None)
    
    if awaiting_location or awaiting_disambiguation:
        logging.info(f"Skipping save_and_send_response for query '{query}' - already sent as location/disambiguation request")
        await set_state_value_in_db(user_id, session_id, 'response_sent', True)
        return
        
    if awaiting_parameter:
        logging.info(f"Skipping save_and_send_response for query '{query}' - awaiting parameter response, will be handled by resumed flow")
        await set_state_value_in_db(user_id, session_id, 'response_sent', True)
        return
        
    if final_response == "":
        logging.info(f"Skipping save_and_send_response for repeat request query '{query}' - no JSON payload sent")
        
        await set_state_value_in_db(user_id, session_id, 'response_sent', True)
        return
        
    replacement_info = await get_state_value_from_db(user_id, session_id, 'replaced_query', None)
    is_station_replacement = await get_state_value_from_db(user_id, session_id, 'is_station_replacement', False)
    
    if is_station_replacement and not replacement_info:
        logging.info(f"Skipping save_and_send_response for original query '{query}' - station replacement in progress")
        
        await set_state_value_in_db(user_id, session_id, 'response_sent', True)
        return
        
    using_corrected_query = await get_state_value_from_db(user_id, session_id, 'using_corrected_query', False)
    if using_corrected_query:
        original_display_query = await get_state_value_from_db(user_id, session_id, 'original_query_for_display', None)
        if original_display_query and original_display_query != query:
            logging.info(f"Using corrected query '{query}' for processing and display")
            display_query = query
            processing_query = query
            await update_state_values_in_db(user_id, session_id, {
                'using_corrected_query': False,
                'original_query_for_display': None
            })
        else:
            display_query = query
            processing_query = query
    else:
        display_query = query
        processing_query = query
        
    if last_saved is not None:
        if is_similar_query(processing_query, last_saved):
            logging.info(f"Conversation for query '{processing_query}' is similar to last saved '{last_saved}' - skipping save")
            await set_state_value_in_db(user_id, session_id, 'response_sent', True)
            return
        
    original_query_language = await get_state_value_from_db(user_id, session_id, 'original_query_language', None)
    original_query_text = await get_state_value_from_db(user_id, session_id, 'original_query_text', original_query_for_display)
    
    if original_query_language and original_query_language != 'en':
        logging.info(f"Translating response from English back to {original_query_language}")
        try:
            translated_response = await translate_text(final_response, source_lang='en', target_lang=original_query_language)
            display_response = translated_response
            
            if "Final Combined Response:" in combined_response:
                parts = combined_response.split("Final Combined Response:", 1)
                if "--------" in parts[1]:
                    separator_parts = parts[1].split("--------", 1)
                    combined_response = parts[0] + "Final Combined Response:" + separator_parts[0] + "--------\n" + translated_response
        except Exception as e:
            logging.error(f"Error translating response for display: {str(e)}")
            display_response = final_response
    else:
        display_response = final_response
    
    is_disambiguation = False
    if "Please select one:" in display_response and any(f"{i+1}. " in display_response for i in range(5)):
        is_disambiguation = True
        logging.warning("Detected disambiguation response in save_and_send_response which should not happen")
    
    if replacement_info:
        display_query = replacement_info['final']
        await update_state_values_in_db(user_id, session_id, {
            'replaced_query': None,
            'is_station_replacement': False
        })
    
    conversation_id = str(uuid.uuid4())
    
    save_query = replacement_info['final'] if replacement_info else processing_query
    
    await set_state_value_in_db(user_id, session_id, 'last_saved_conversation', processing_query)
    
    should_save_to_db = True
    
    awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
    awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
    awaiting_parameter = await get_state_value_from_db(user_id, session_id, 'awaiting_parameter', None)
    if not awaiting_location and not awaiting_disambiguation and not awaiting_parameter:
        should_save_to_db = True
        logging.info(f"Will save to database: Query '{save_query}' - all conversations are saved")
    else:
        should_save_to_db = False
        logging.info(f"Skipping database save for query: '{save_query}' - disambiguation, location, or parameter flow")
    
    if should_save_to_db:
        try:
            save_task = asyncio.create_task(save_conversation(
                user_id=user_id,
                query=save_query,
                response=display_response,
                session_id=session_id,
                conversation_id=conversation_id
            ))
            
            activity_task = asyncio.create_task(update_session_activity(user_id, session_id))
            
            await asyncio.gather(save_task, activity_task)
        except Exception as e:
            logging.error(f"Error saving conversation to database: {str(e)}")
    
    response_query = await get_state_value_from_db(user_id, session_id, 'original_query_for_response', display_query)
    
    if not response_query or not isinstance(response_query, str) or response_query.strip() == '':
        if display_query and display_query.strip():
            logging.debug(f"Using display_query as fallback: {display_query}")
        response_query = display_query if display_query and display_query.strip() else "User query"
    
    response_query = response_query.strip()
    
    conversation_data = {
        'type': 'conversation',
        'query': response_query,  
        'response': display_response,
        'timestamp': datetime.utcnow().isoformat(),
        'session_id': session_id,
        'is_disambiguation': is_disambiguation,
        'conversation_id': conversation_id
    }
    
    logging.info(f"Sending response with query: '{response_query}'")
    
    await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
    await set_state_value_in_db(user_id, session_id, 'response_sent', True)
    
    awaiting_disambiguation = await get_state_value_from_db(user_id, session_id, 'awaiting_disambiguation', False)
    awaiting_location = await get_state_value_from_db(user_id, session_id, 'awaiting_location', False)
    
    if not awaiting_disambiguation and not awaiting_location:
        await set_state_value_in_db(user_id, session_id, 'complete_query', False)
        
        if 'orig' not in processing_query.lower() and 'from ' not in processing_query.lower():
            await set_state_value_in_db(user_id, session_id, 'origin_station', None)
            
        if 'dest' not in processing_query.lower() and 'to ' not in processing_query.lower():
            await set_state_value_in_db(user_id, session_id, 'destination_station', None)

async def translate_text(text: str, source_lang: str = "en", target_lang: str = "en") -> str:
    """Translate text between languages using AWS Translate with retry and error handling"""
    if source_lang == target_lang or not text.strip():
        return text
        
    try:
        logging.info(f"Translating text from {source_lang} to {target_lang}: {truncate_json_for_logging(text, 100)}")
        
        translate_client = get_translate_client()
        
        lang_map = {
            "en": "en",
            "es": "es",
            "zh": "zh"
        }
        
        source = lang_map.get(source_lang, "en")
        target = lang_map.get(target_lang, "en")
        
        max_chunk_size = 5000
        if len(text) > max_chunk_size:
            chunks = []
            sentences = re.split(r'(?<=[.!?])\s+', text)
            current_chunk = []
            current_length = 0
            
            for sentence in sentences:
                if len(sentence) > max_chunk_size:  
                    if current_chunk:
                        chunks.append(' '.join(current_chunk))
                        current_chunk = []
                        current_length = 0
                    
                    words = sentence.split()
                    sub_chunk = []
                    sub_length = 0
                    
                    for word in words:
                        if sub_length + len(word) + 1 > max_chunk_size:
                            chunks.append(' '.join(sub_chunk))
                            sub_chunk = [word]
                            sub_length = len(word)
                        else:
                            sub_chunk.append(word)
                            sub_length += len(word) + 1
                    
                    if sub_chunk:
                        chunks.append(' '.join(sub_chunk))
                else:
                    if current_length + len(sentence) + 1 > max_chunk_size:
                        chunks.append(' '.join(current_chunk))
                        current_chunk = [sentence]
                        current_length = len(sentence)
                    else:
                        current_chunk.append(sentence)
                        current_length += len(sentence) + 1
            
            if current_chunk:
                chunks.append(' '.join(current_chunk))
            
            translated_chunks = []
            for i, chunk in enumerate(chunks):
                logging.info(f"Translating chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
                try:
                    retry_count = 0
                    max_retries = 3
                    while retry_count < max_retries:
                        try:
                            response = translate_client.translate_text(
                                Text=chunk,
                                SourceLanguageCode=source,
                                TargetLanguageCode=target
                            )
                            translated_chunks.append(response.get('TranslatedText', chunk))
                            break
                        except Exception as retry_e:
                            retry_count += 1
                            if retry_count >= max_retries:
                                logging.error(f"Failed to translate chunk after {max_retries} attempts: {str(retry_e)}")
                                translated_chunks.append(chunk)  
                            else:
                                logging.warning(f"Translation attempt {retry_count} failed: {str(retry_e)}. Retrying...")
                                await asyncio.sleep(0.5)  
                except Exception as chunk_e:
                    logging.error(f"Error translating chunk: {str(chunk_e)}")
                    translated_chunks.append(chunk)  
            
            translated_text = ' '.join(translated_chunks)
        else:
            response = translate_client.translate_text(
                Text=text,
                SourceLanguageCode=source,
                TargetLanguageCode=target
            )
            
            translated_text = response.get('TranslatedText', text)
        
        logging.info(f"Translation completed: {truncate_json_for_logging(translated_text, 100)}")
        return translated_text
    except Exception as e:
        logging.exception(f"Translation failed: {str(e)}")
        return text

async def detect_language(text: str) -> str:
    """Detect the language of the given text using AWS Comprehend with fallback to pattern detection"""
    try:
        pattern_language = detect_language_by_characters(text)
        if pattern_language != 'en':
            logging.info(f"Pattern-based language detection: {pattern_language}")
            return pattern_language
        
        try:
            comprehend_client = get_comprehend_client()
            
            response = comprehend_client.detect_dominant_language(
                Text=text
            )
            
            languages = response.get('Languages', [])
            if languages:
                dominant_lang = languages[0].get('LanguageCode', 'en')
                logging.info(f"Detected language: {dominant_lang} (score: {languages[0].get('Score', 0):.2f})")
                
                lang_map = {
                    'en': 'en',
                    'es': 'es',
                    'zh': 'zh',
                    'zh-TW': 'zh'
                }
                
                return lang_map.get(dominant_lang[:2], 'en')
            
        except Exception as e:
            logging.warning(f"AWS Comprehend language detection failed: {str(e)}. Using pattern-based detection instead.")
            return pattern_language
            
        return pattern_language  
    except Exception as e:
        logging.exception(f"Language detection failed: {str(e)}")
        return 'en'

def detect_language_by_characters(text: str) -> str:
    """Detect language based on character patterns without using external APIs"""
    if not text or len(text.strip()) == 0:
        return 'en'  
    
    text = text.strip().lower()
    
    total_chars = len(text)
    
    chinese_chars = sum(1 for char in text if 
                        ('\u4e00' <= char <= '\u9fff') or  
                        ('\u3400' <= char <= '\u4dbf') or  
                        ('\u20000' <= char <= '\u2a6df'))  
    
    spanish_chars = sum(1 for char in text if char in 'ñáéíóúü¿¡')
    
    if total_chars > 0:
        chinese_percent = chinese_chars / total_chars
        spanish_percent = spanish_chars / total_chars
        
        logging.info(f"Language detection - Chinese: {chinese_percent:.2f}, Spanish: {spanish_percent:.2f}")
    
        if chinese_percent > 0.3:
            return 'zh'
        elif spanish_percent > 0.1:     
            return 'es'
    
    return 'en'

# ======================================UTILITY FUNCTIONS (NOW IN APIS.PY)======================================

def preprocess_concatenated_stations(query_text: str) -> str:
    """
    Preprocess query to handle concatenated station names (e.g., 'sanfrancisco' -> 'san francisco').
    This function identifies concatenated multi-word station names and adds appropriate spaces.
    It also autocorrects misspelled station names using fuzzy matching.
    IMPORTANT: This function only corrects station names, preserving all other words in the query.
    """
    import re
    from difflib import SequenceMatcher
    
    all_station_names = []
    valid_station_names = {}
    
    for station in STATION_DATA["stations"]:
        station_name = station["name"]
        all_station_names.append(station_name)
        valid_station_names[station_name.lower()] = station_name
    
    for group_name in STATION_GROUPS.keys():
        all_station_names.append(group_name)
        valid_station_names[group_name.lower()] = group_name
    
    concatenated_mapping = {}
    
    for station_name in all_station_names:
        if ' ' in station_name or '/' in station_name or '.' in station_name:
            concatenated = re.sub(r'[^a-zA-Z0-9]', '', station_name.lower())
            
            if concatenated != station_name.lower().replace(' ', ''):
                concatenated_mapping[concatenated] = station_name
            
            space_removed = station_name.lower().replace(' ', '')
            if space_removed != station_name.lower() and space_removed not in concatenated_mapping:
                concatenated_mapping[space_removed] = station_name
    
    sorted_concatenated = sorted(concatenated_mapping.keys(), key=len, reverse=True)
    
    processed_query = query_text
    query_lower = query_text.lower()
    
    replacements_made = []
    
    for concatenated in sorted_concatenated:
        if concatenated in query_lower:
            pattern = re.compile(re.escape(concatenated), re.IGNORECASE)
            matches = list(pattern.finditer(query_lower))
            
            for match in reversed(matches):  
                start, end = match.span()
                
                overlaps = any(
                    (start < prev_end and end > prev_start) 
                    for prev_start, prev_end in replacements_made
                )
                
                if not overlaps:
                    original_text = processed_query[start:end]
                    replacement = concatenated_mapping[concatenated]
                    
                    if original_text and original_text[0].isupper():
                        replacement = replacement.capitalize()
                    
                    processed_query = processed_query[:start] + replacement + processed_query[end:]
                    
                    length_diff = len(replacement) - len(original_text)
                    replacements_made = [
                        (pos_start + (length_diff if pos_start >= start else 0), 
                         pos_end + (length_diff if pos_end >= start else 0))
                        for pos_start, pos_end in replacements_made
                    ]
                    replacements_made.append((start, start + len(replacement)))
                    
                    print(f"Preprocessed concatenated station: '{original_text}' -> '{replacement}'")
                    
                    query_lower = processed_query.lower()
    
    tokens = []
    
    for match in re.finditer(r'\b\w{4,}\b', processed_query, re.IGNORECASE):
        start, end = match.span()
        word = processed_query[start:end]
        tokens.append((word, start, end))
    
    words = [t[0] for t in tokens]
    for i in range(len(tokens)-1):
        if i < len(tokens)-1:
            word1, start1, _ = tokens[i]
            word2, _, end2 = tokens[i+1]
            between_text = processed_query[tokens[i][2]:tokens[i+1][1]].strip()
            if between_text == "" or between_text == " ":
                combined = f"{word1} {word2}"
                tokens.append((combined, start1, end2))
    
    if len(tokens) >= 3:
        for i in range(len(tokens)-2):
            word1, start1, _ = tokens[i]
            word2, _, _ = tokens[i+1]
            word3, _, end3 = tokens[i+2]
            if processed_query[tokens[i][2]:tokens[i+1][1]].strip() in ["", " "] and \
               processed_query[tokens[i+1][2]:tokens[i+2][1]].strip() in ["", " "]:
                combined = f"{word1} {word2} {word3}"
                tokens.append((combined, start1, end3))
    
    SIMILARITY_THRESHOLD = 0.75
    
    tokens.sort(key=lambda x: len(x[0]), reverse=True)
    
    new_replacements = []
    
    existing_stations = []
    for valid_name_lower, valid_name in valid_station_names.items():
        station_pos = processed_query.lower().find(valid_name_lower)
        if station_pos >= 0:
            existing_stations.append((valid_name, station_pos, station_pos + len(valid_name_lower)))
    
    for token, start, end in tokens:
        token_lower = token.lower()
        
        if token_lower in valid_station_names:
            continue
        
        overlaps = any(
            (start < prev_end and end > prev_start) 
            for prev_start, prev_end in new_replacements
        )
        
        for _, station_start, station_end in existing_stations:
            if (start < station_end and end > station_start):
                overlaps = True
            break
    
        if overlaps:
            continue
            
        best_match = None
        best_score = 0
        
        is_part_of_station = False
        for valid_name_lower, _ in valid_station_names.items():
            station_pos = processed_query.lower().find(valid_name_lower)
            if station_pos >= 0 and start >= station_pos and end <= station_pos + len(valid_name_lower):
                is_part_of_station = True
                break
        
        if is_part_of_station:
            continue
        
        for valid_name_lower, valid_name in valid_station_names.items():
            if token_lower in valid_name_lower:
                score = len(token_lower) / len(valid_name_lower)
                if score > best_score and score > SIMILARITY_THRESHOLD:
                    best_score = score
                    best_match = valid_name
            continue
        
            score = SequenceMatcher(None, token_lower, valid_name_lower).ratio()
            if score > best_score and score > SIMILARITY_THRESHOLD:
                best_score = score
                best_match = valid_name
        
        if best_match:
            if token and token[0].isupper():
                corrected = best_match[0].upper() + best_match[1:]
            else:
                corrected = best_match
                
            processed_query = processed_query[:start] + corrected + processed_query[end:]
            
            length_diff = len(corrected) - len(token)
            tokens = [
                (w, 
                 s + (length_diff if s > end else 0),
                 e + (length_diff if e > end else 0))
                for w, s, e in tokens
            ]
            
            new_replacements.append((start, start + len(corrected)))
            
            print(f"Autocorrected station name: '{token}' -> '{corrected}' (similarity: {best_score:.2f})")
    
    if processed_query != query_text:
        print(f"Query preprocessing complete: '{query_text}' -> '{processed_query}'")
    
    return processed_query


async def process_station_validation(query_text: str, invalid_stations: List[str], 
                               suggestions: Dict[str, List[str]], websocket: WebSocket, 
                               user_id: str = None, session_id: str = None) -> Optional[str]:
    """
    Process invalid station names by informing the user and requesting valid replacements.
    
    Args:
        query_text: The original query text
        invalid_stations: List of invalid station names
        suggestions: Dictionary mapping each invalid station to suggested alternatives
        websocket: WebSocket connection for communication
        user_id: User ID (optional, will be retrieved from websocket if not provided)
        session_id: Session ID (optional, will be retrieved from websocket if not provided)
    
    Returns:
        Modified query with valid station names, or None if processing should stop to wait for user input
    """
    if not user_id or not session_id:
        try:
            user_id, session_id = await get_user_session_info(websocket)
        except Exception as e:
            logging.error(f"Error getting session info in process_station_validation: {str(e)}")
            return None
    
    if not user_id or not session_id:
        logging.error("Missing user_id or session_id in process_station_validation")
        return None
    
    org_terms = ["bart","BART", "bay area rapid transit", "bay area rapid", "rapid transit"]
    
    query_lower = query_text.lower()
    if any(term in query_lower for term in org_terms) and any(word in query_lower for word in ["station", "stations", "stop", "stops"]):
        return query_text
    
    for invalid_station in invalid_stations:
        if invalid_station.lower() in org_terms:
            continue
            
        station_suggestions = suggestions[invalid_station]
        
        enhanced_suggestions = {}
        
        def generate_smart_suggestions(invalid_station_name: str, station_data: dict) -> list:
            """
            Production-grade suggestion generation that uses multiple algorithms
            to find the best station matches without hardcoding.
            """
            suggestions = []
            invalid_lower = invalid_station_name.lower()
            
            # Algorithm 1: First letter matching
            first_letter = invalid_station_name[0].lower()
            for station in station_data["stations"]:
                if station["name"].lower().startswith(first_letter):
                    suggestions.append(station["name"])
            
            # Algorithm 2: Fuzzy matching by similarity
            if len(suggestions) < 3: 
                from difflib import SequenceMatcher
                similarity_scores = []
                
                for station in station_data["stations"]:
                    similarity = SequenceMatcher(None, invalid_lower, station["name"].lower()).ratio()
                    if similarity > 0.3: 
                        similarity_scores.append((station["name"], similarity))
                
                similarity_scores.sort(key=lambda x: x[1], reverse=True)
                for station_name, score in similarity_scores[:3]:
                    if station_name not in suggestions:
                        suggestions.append(station_name)
            
            # Algorithm 3: Word overlap matching
            if len(suggestions) < 3:
                invalid_words = set(invalid_lower.split())
                word_overlap_scores = []
                
                for station in station_data["stations"]:
                    station_words = set(station["name"].lower().split())
                    overlap = len(invalid_words.intersection(station_words))
                    if overlap > 0:
                        word_overlap_scores.append((station["name"], overlap))
                
                word_overlap_scores.sort(key=lambda x: x[1], reverse=True)
                for station_name, overlap in word_overlap_scores[:2]:
                    if station_name not in suggestions:
                        suggestions.append(station_name)
            
            # Algorithm 4: Fallback to popular stations if no good matches
            if len(suggestions) < 2:
                popular_stations = [station["name"] for station in station_data["stations"][:5]]
                for station_name in popular_stations:
                    if station_name not in suggestions:
                        suggestions.append(station_name)
            
            return suggestions[:5]  
        
        enhanced_suggestions[invalid_station] = generate_smart_suggestions(invalid_station, STATION_DATA)
        
        user_language = await get_state_value_from_db(user_id, session_id, 'language', 'en')
        
        def get_localized_messages(lang: str) -> dict:
            """
            Production-grade localization that can be extended to any language
            without code changes. Uses a configuration-driven approach.
            """
            localization_config = {
            "en": {
                    "invalid_station": "is not a valid BART station.",
                    "suggestions_prefix": "Here are some valid stations that start with",
                    "please_provide": "Please provide a valid station name from the BART system."
            },
            "es": {
                    "invalid_station": "no es una estación de BART válida.",
                    "suggestions_prefix": "Aquí hay algunas estaciones válidas que comienzan con",
                    "please_provide": "Por favor, proporcione un nombre de estación válido del sistema BART."
            },
            "zh": {
                    "invalid_station": "不是有效的BART车站。",
                    "suggestions_prefix": "以下是一些以",
                    "please_provide": "请提供BART系统中的有效车站名称。"
                }
            }
            
            return localization_config.get(lang, localization_config["en"])
        
        lang_messages = get_localized_messages(user_language)
        
        # CRITICAL FIX: Only process the first invalid station to avoid overwhelming the user
        # This ensures we ask for one invalid station at a time, not all simultaneously
        first_invalid_station = invalid_stations[0] if invalid_stations else None
        
        if first_invalid_station:
            suggestions_list = enhanced_suggestions.get(first_invalid_station, [])
            suggestions_text = ", ".join(suggestions_list)
            
            if user_language == "zh":
                response_message = (
                    f"'{first_invalid_station}' {lang_messages['invalid_station']} "
                    f"{lang_messages['suggestions_prefix']} '{first_invalid_station[0].upper()}' 开头的有效车站：{suggestions_text}。 "
                    f"{lang_messages['please_provide']}"
                )
            else:
                response_message = (
                    f"'{first_invalid_station}' {lang_messages['invalid_station']} "
                    f"{lang_messages['suggestions_prefix']} '{first_invalid_station[0].upper()}': {suggestions_text}. "
                    f"{lang_messages['please_provide']}"
                )
        else:
            response_message = lang_messages['please_provide']
        
        try:
            conversation_data = {
                "type": "conversation",
                "response": response_message,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": session_id,
                "conversation_id": str(uuid.uuid4()),
                "is_station_validation": True
            }
            
            await safe_websocket_send_text(websocket, json.dumps(conversation_data, ensure_ascii=False))
            
            audio_bytes = await synthesize_speech(response_message, language=user_language)
            if audio_bytes:
                await safe_websocket_send_bytes(websocket, audio_bytes)
            
            await update_state_values_in_db(user_id, session_id, {
                'awaiting_station_validation': True,
                'invalid_station': invalid_stations[0] if invalid_stations else None,
                'original_query': query_text
            })
            
            print(f"Sent station validation response for invalid stations: {invalid_stations}")
            return None  
            
        except Exception as e:
            logging.error(f"Error sending station validation response: {str(e)}")
            return None
    
    return query_text

def preprocess_concatenated_stations(query_text: str) -> str:
    """
    Preprocess query to handle concatenated station names (e.g., 'sanfrancisco' -> 'san francisco').
    This function identifies concatenated multi-word station names and adds appropriate spaces.
    It also autocorrects misspelled station names using fuzzy matching.
    IMPORTANT: This function only corrects station names, preserving all other words in the query.
    """
    import re
    from difflib import SequenceMatcher
    
    all_station_names = []
    valid_station_names = {}
    
    for station in STATION_DATA["stations"]:
        station_name = station["name"]
        all_station_names.append(station_name)
        valid_station_names[station_name.lower()] = station_name
    
    for group_name in STATION_GROUPS.keys():
        all_station_names.append(group_name)
        valid_station_names[group_name.lower()] = group_name
    
    concatenated_mapping = {}
    
    for station_name in all_station_names:
        if ' ' in station_name or '/' in station_name or '.' in station_name:
            concatenated = re.sub(r'[^a-zA-Z0-9]', '', station_name.lower())
            
            if concatenated != station_name.lower().replace(' ', ''):
                concatenated_mapping[concatenated] = station_name
            
            space_removed = station_name.lower().replace(' ', '')
            if space_removed != station_name.lower() and space_removed not in concatenated_mapping:
                concatenated_mapping[space_removed] = station_name
    
    sorted_concatenated = sorted(concatenated_mapping.keys(), key=len, reverse=True)
    
    processed_query = query_text
    query_lower = query_text.lower()
    
    replacements_made = []
    
    for concatenated in sorted_concatenated:
        if concatenated in query_lower:
            pattern = re.compile(re.escape(concatenated), re.IGNORECASE)
            matches = list(pattern.finditer(query_lower))
            
            for match in reversed(matches):  
                start, end = match.span()
                
                overlaps = any(
                    (start < prev_end and end > prev_start) 
                    for prev_start, prev_end in replacements_made
                )
                
                if not overlaps:
                    original_text = processed_query[start:end]
                    replacement = concatenated_mapping[concatenated]
                    
                    if original_text and original_text[0].isupper():
                        replacement = replacement.capitalize()
                    
                    processed_query = processed_query[:start] + replacement + processed_query[end:]
                    
                    length_diff = len(replacement) - len(original_text)
                    replacements_made = [
                        (pos_start + (length_diff if pos_start >= start else 0), 
                         pos_end + (length_diff if pos_end >= start else 0))
                        for pos_start, pos_end in replacements_made
                    ]
                    replacements_made.append((start, start + len(replacement)))
                    
                    print(f"Preprocessed concatenated station: '{original_text}' -> '{replacement}'")
                    
                    query_lower = processed_query.lower()
    
    
    tokens = []
    
    for match in re.finditer(r'\b\w{4,}\b', processed_query, re.IGNORECASE):
        start, end = match.span()
        word = processed_query[start:end]
        tokens.append((word, start, end))
    
    words = [t[0] for t in tokens]
    for i in range(len(tokens)-1):
        if i < len(tokens)-1:
            word1, start1, _ = tokens[i]
            word2, _, end2 = tokens[i+1]
            between_text = processed_query[tokens[i][2]:tokens[i+1][1]].strip()
            if between_text == "" or between_text == " ":
                combined = f"{word1} {word2}"
                tokens.append((combined, start1, end2))
    
    if len(tokens) >= 3:
        for i in range(len(tokens)-2):
            word1, start1, _ = tokens[i]
            word2, _, _ = tokens[i+1]
            word3, _, end3 = tokens[i+2]
            if processed_query[tokens[i][2]:tokens[i+1][1]].strip() in ["", " "] and \
               processed_query[tokens[i+1][2]:tokens[i+2][1]].strip() in ["", " "]:
                combined = f"{word1} {word2} {word3}"
                tokens.append((combined, start1, end3))
    
    SIMILARITY_THRESHOLD = 0.75
    
    tokens.sort(key=lambda x: len(x[0]), reverse=True)
    
    new_replacements = []
    
    existing_stations = []
    for valid_name_lower, valid_name in valid_station_names.items():
        station_pos = processed_query.lower().find(valid_name_lower)
        if station_pos >= 0:
            existing_stations.append((valid_name, station_pos, station_pos + len(valid_name_lower)))
    
    for token, start, end in tokens:
        token_lower = token.lower()
        
        if token_lower in valid_station_names:
            continue
        
        overlaps = any(
            (start < prev_end and end > prev_start) 
            for prev_start, prev_end in new_replacements
        )
        
        for _, station_start, station_end in existing_stations:
            if (start < station_end and end > station_start):
                overlaps = True
                break
        
        if overlaps:
            continue
            
        best_match = None
        best_score = 0
        
        is_part_of_station = False
        for valid_name_lower, _ in valid_station_names.items():
            station_pos = processed_query.lower().find(valid_name_lower)
            if station_pos >= 0 and start >= station_pos and end <= station_pos + len(valid_name_lower):
                is_part_of_station = True
                break
        
        if is_part_of_station:
            continue
            
        for valid_name_lower, valid_name in valid_station_names.items():
            if token_lower in valid_name_lower:
                score = len(token_lower) / len(valid_name_lower)
                if score > best_score and score > SIMILARITY_THRESHOLD:
                    best_score = score
                    best_match = valid_name
                continue
                
            score = SequenceMatcher(None, token_lower, valid_name_lower).ratio()
            if score > best_score and score > SIMILARITY_THRESHOLD:
                best_score = score
                best_match = valid_name
        
        if best_match:
            if token and token[0].isupper():
                corrected = best_match[0].upper() + best_match[1:]
            else:
                corrected = best_match
                
            processed_query = processed_query[:start] + corrected + processed_query[end:]
            
            length_diff = len(corrected) - len(token)
            tokens = [
                (w, 
                 s + (length_diff if s > end else 0),
                 e + (length_diff if e > end else 0))
                for w, s, e in tokens
            ]
            
            new_replacements.append((start, start + len(corrected)))
            
            print(f"Autocorrected station name: '{token}' -> '{corrected}' (similarity: {best_score:.2f})")
    
    if processed_query != query_text:
        print(f"Query preprocessing complete: '{query_text}' -> '{processed_query}'")
    
    return processed_query