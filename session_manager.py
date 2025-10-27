#======================================IMPORTS======================================
import asyncio
import json
import re
import boto3
import logging
import requests
import random
import websockets
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException,status,Response
from fastapi.middleware.cors import CORSMiddleware
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.model import AudioStream, TranscriptEvent
from botocore.response import StreamingBody
from typing import Dict, Any, Optional, List, Tuple
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.responses import JSONResponse
import pprint
from datetime import datetime, timedelta
import uuid
import pytz
import collections
import traceback
import os
import time
import threading

# Import models
from models import SessionInfo, QueryContext, SessionContext

# Import database functions from database_operations
from database_operations import (
    get_user_profile_from_db, update_user_profile_in_db, save_session_info, 
    update_session_activity, get_previous_conversations, save_context_to_db,
    get_context_from_db, update_context_in_db, delete_context_from_db,
    cleanup_expired_contexts_from_db
)

# Import context manager from context_manager
from context_manager import context_manager
#======================================SESSION MANAGEMENT======================================
def extract_parameters_from_query(query_text: str, intent_data: Dict[str, Any]) -> Dict[str, Any]:
    """Enhanced parameter extraction from query and intent data"""
    params = intent_data.get("parameters", {}).copy()
    
    # Additional parameter extraction logic
    query_lower = query_text.lower()
    
    # Extract time-related parameters
    if "now" in query_lower:
        params["time"] = "now"
    
    # Extract date-related parameters  
    date_patterns = {
        "today": "today",
        "weekday": "wd", 
        "weekend": "sa",
        "saturday": "sa",
        "sunday": "su"
    }
    
    for pattern, value in date_patterns.items():
        if pattern in query_lower:
            params["date"] = value
            break
    
    # Extract platform information
    platform_match = re.search(r'platform\s+(\d+)', query_lower)
    if platform_match:
        params["plat"] = platform_match.group(1)
    
    # Extract direction - but NOT from station names
    # Only extract direction if it's explicitly mentioned as a direction, not as part of a station name
    direction_patterns = [
        r'\bnorthbound\b',
        r'\bsouthbound\b', 
        r'\bnorth\s+(bound|direction|trains?)\b',
        r'\bsouth\s+(bound|direction|trains?)\b'
    ]
    
    has_direction = False
    for pattern in direction_patterns:
        if re.search(pattern, query_lower):
            has_direction = True
            break
    
    if has_direction:
        if "north" in query_lower:
            params["dir"] = "n"
        elif "south" in query_lower:
            params["dir"] = "s"
    
    # Map station parameters to actual station names
    for key, value in params.items():
        if key in ['orig', 'dest', 'station'] and value:
            # Note: This function needs to be imported from main.py
            # mapped_value = map_user_station_to_actual_station(str(value))
            # if mapped_value != value:
            #     print(f"Mapped station parameter in extraction: {key}='{value}' → '{mapped_value}'")
            #     params[key] = mapped_value
            pass
    
    # Clean up None values
    return {k: v for k, v in params.items() if v is not None}

# NOTE: get_endpoint_param_schema() and merge_parameters_with_context() were removed
# PRODUCTION PHILOSOPHY (Oct 2025):
# - AI Intent Classification already receives full context in the prompt
# - Therefore, parameters it returns are already context-aware and correct
# - No need for context-based parameter fallback mechanisms
# - Only handle: (1) Mandatory defaults when missing, (2) Validate required params

# ======================================DATABASE SESSION MANAGER======================================
class DatabaseSessionManager:
    """Database-only session manager with TTL-based expiration"""
    
    def __init__(self, session_timeout_minutes: int = None):
        import os
        timeout_minutes = session_timeout_minutes or int(os.getenv("SESSION_TIMEOUT_MINUTES"))
        self.session_timeout = timedelta(minutes=timeout_minutes)
        
        logging.info(f"DatabaseSessionManager initialized - {session_timeout_minutes} minute TTL expiration")
        
        # No background cleanup needed - DynamoDB TTL handles expiration automatically
    
    async def create_session(self, user_id: str, session_id: str, 
                           language: str = 'en') -> bool:
        """Create a new session in database with TTL"""
        try:
            # Save session to database
            success = await save_session_info(user_id, session_id, language)
            
            if success:
                logging.info(f"Created session {user_id}:{session_id} in database with TTL")
                
                # Also save user profile
                await save_user_profile_to_db(user_id, language)
                
                # Initialize empty WebSocket state in DB
                from database_operations import save_ws_state_to_db
                initial_state = {
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
                    'skip_tts': False,
                    'is_health_check': False,
                    'is_repeat_request': False,
                    'is_station_replacement': False,
                    'location_response_processed': False
                }
                await save_ws_state_to_db(user_id, session_id, initial_state)
                
                return True
            else:
                logging.error(f"Failed to create session {user_id}:{session_id}")
                return False
                
        except Exception as e:
            logging.error(f"Error creating session: {str(e)}")
            return False
    
    async def validate_session(self, user_id: str, session_id: str) -> Tuple[bool, Optional[str]]:
        """Validate session from database with TTL check"""
        return await validate_session_in_db(user_id, session_id)
    
    async def get_session(self, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session information from database"""
        return await get_session_from_db(user_id, session_id)
    
    async def expire_session(self, user_id: str, session_id: str) -> bool:
        """Manually expire a session by deleting from database"""
        return await delete_session_from_db(user_id, session_id)
    
    async def remove_session(self, user_id: str, session_id: str) -> bool:
        """Remove session completely from database"""
        return await delete_session_from_db(user_id, session_id)
    
    async def get_active_sessions(self) -> Dict[str, Dict]:
        """Get all active sessions from database"""
        return await get_active_sessions_from_db()
    
    async def update_session_activity(self, user_id: str, session_id: str) -> bool:
        """Update session activity timestamp in the database"""
        return await update_session_activity(user_id, session_id)
    
    async def regenerate_session(self, user_id: str, old_session_id: str, 
                               language: str = 'en') -> Tuple[str, bool]:
        """Regenerate session for expired but active user"""
        try:
            # Delete old session
            await delete_session_from_db(user_id, old_session_id)
            logging.info(f"Removed expired session {user_id}:{old_session_id}")
            
            # Create new session
            new_session_id = str(uuid.uuid4())
            success = await self.create_session(
                user_id=user_id,
                session_id=new_session_id,
                language=language
            )
            
            if success:
                # Update session activity
                await self.update_session_activity(user_id, new_session_id)
                logging.info(f"Regenerated session for user {user_id}: {old_session_id} -> {new_session_id}")
                return new_session_id, True
            else:
                logging.error(f"Failed to regenerate session for user {user_id}")
                return "", False
                
        except Exception as e:
            logging.error(f"Error regenerating session: {str(e)}")
            return "", False
#======================================SESSION HELPER FUNCTIONS======================================
async def validate_session_and_regenerate_if_needed(user_id: str, session_id: str) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """
    Validate session and automatically regenerate if expired
    
    Returns:
        Tuple[bool, str, Optional[str], Optional[str]]: (is_valid, error_message, language, new_session_id)
    """
    try:
        logging.info(f"Starting session validation for {user_id}:{session_id}")
        
        # Validate session exists and is active
        is_valid, error_msg, language = await validate_session_in_db(user_id, session_id)
        logging.info(f"Session validation result: is_valid={is_valid}, error_msg={error_msg}")
        
        if is_valid:
            # logging.info(f"Session validated for {user_id}:{session_id}")  # Removed for cleaner logs
            return True, "", language, None
        
        # Session is invalid - check if we should regenerate
        # Only regenerate for expired sessions, not for non-existent sessions
        if error_msg == "Session expired":
            logging.info(f"Session {session_id} for user {user_id} is expired, regenerating session")
            
            try:
                # Get language from old session if possible
                old_language = 'en'
                if error_msg == "Session expired":
                    session_data, _ = await get_session_from_db(user_id, session_id)
                    if session_data:
                        old_language = session_data.get('language', 'en')
                        logging.info(f"Retrieved language from expired session: {old_language}")
                
                # Create new session
                new_session_id = str(uuid.uuid4())
                logging.info(f"Creating new session: {user_id}:{new_session_id}")
                success = await save_session_info(user_id, new_session_id, old_language)
                
                if success:
                    logging.info(f"Successfully regenerated session for {user_id}: {session_id} -> {new_session_id}")
                    return True, "Session regenerated", old_language, new_session_id
                else:
                    logging.error(f"Failed to regenerate session for {user_id}")
                    return False, "Failed to regenerate session", None, None
                    
            except Exception as e:
                logging.error(f"Error regenerating session: {str(e)}")
                return False, f"Session regeneration error: {str(e)}", None, None
        else:
            logging.warning(f"Session validation failed for {user_id}:{session_id} - {error_msg}")
            return False, error_msg, None, None
        
    except Exception as e:
        error_msg = f"Session validation error: {str(e)}"
        logging.error(error_msg)
        return False, error_msg, None, None

async def validate_session_for_query(user_id: str, session_id: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate session for query processing - no activity updates
    
    Returns:
        Tuple[bool, str, Optional[str]]: (is_valid, error_message, language)
    """
    try:
        # Validate session exists and is active
        is_valid, error_msg, language = await validate_session_in_db(user_id, session_id)
        
        if not is_valid:
            logging.warning(f"Session validation failed for {user_id}:{session_id} - {error_msg}")
            return False, error_msg, None
        
        # logging.info(f"Session validated for {user_id}:{session_id}")  # Removed for cleaner logs
        return True, "", language
        
    except Exception as e:
        error_msg = f"Session validation error: {str(e)}"
        logging.error(error_msg)
        return False, error_msg, None

#======================================DATABASE FUNCTIONS======================================
async def get_session_from_db(user_id: str, session_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Get session information from DynamoDB"""
    try:
        from aws_client_manager import get_dynamodb_resource
        dynamodb = get_dynamodb_resource()
        import os
        table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
        
        response = table.get_item(
            Key={
                'user_id': user_id,
                'record_type': f'sessions/{user_id}/{session_id}'
            }
        )
        
        if 'Item' in response:
            item = response['Item']
            logging.info(f"Retrieved session from DB: {user_id}:{session_id}")
            return item, None
        else:
            logging.info(f"Session not found in DB: {user_id}:{session_id}")
            return None, "Session not found"
            
    except Exception as e:
        error_msg = f"Database error getting session: {str(e)}"
        logging.error(error_msg)
        return None, error_msg

async def validate_session_in_db(user_id: str, session_id: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validate session from database with TTL check"""
    try:
        session_data, error = await get_session_from_db(user_id, session_id)
        
        if error:
            return False, error, None
        
        if not session_data:
            return False, "Session not found", None
        
        # Check if session is expired using TTL (using PDT timezone for consistency)
        ttl = session_data.get('ttl')
        if ttl:
            current_time = int(datetime.now(pytz.timezone('US/Pacific')).timestamp())
            if current_time > ttl:
                logging.info(f"Session expired: {user_id}:{session_id} (TTL: {ttl}, current: {current_time})")
                return False, "Session expired", None
        
        # Session is valid
        language = session_data.get('language', 'en')
        # logging.info(f"Session validated: {user_id}:{session_id}")  # Removed for cleaner logs
        return True, None, language
        
    except Exception as e:
        error_msg = f"Session validation error: {str(e)}"
        logging.error(error_msg)
        return False, error_msg, None

async def delete_session_from_db(user_id: str, session_id: str) -> bool:
    """Delete session from DynamoDB"""
    try:
        from aws_client_manager import get_dynamodb_resource
        dynamodb = get_dynamodb_resource()
        import os
        table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
        
        table.delete_item(
            Key={
                'user_id': user_id,
                'record_type': f'sessions/{user_id}/{session_id}'
            }
        )
        
        logging.info(f"Deleted session from DB: {user_id}:{session_id}")
        return True
        
    except Exception as e:
        logging.error(f"Database error deleting session: {str(e)}")
        return False

async def get_active_sessions_from_db() -> Dict[str, Dict]:
    """Get all active sessions from database"""
    try:
        from aws_client_manager import get_dynamodb_resource
        dynamodb = get_dynamodb_resource()
        import os
        table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
        
        response = table.scan()
        sessions = {}
        
        for item in response.get('Items', []):
            user_id = item.get('user_id')
            record_type = item.get('record_type', '')
            if user_id and record_type.startswith('sessions/'):
                # Extract session_id from record_type
                session_id = record_type.split('/')[-1]
                sessions[f"{user_id}:{session_id}"] = item
        
        logging.info(f"Retrieved {len(sessions)} active sessions from DB")
        return sessions
        
    except Exception as e:
        logging.error(f"Database error getting active sessions: {str(e)}")
        return {}



async def save_user_profile_to_db(user_id: str, language: str = 'en') -> bool:
    """Save user profile to DynamoDB"""
    try:
        from aws_client_manager import get_dynamodb_resource
        dynamodb = get_dynamodb_resource()
        import os
        table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
        
        table.put_item(
            Item={
                'user_id': user_id,
                'record_type': f'profiles/{user_id}',
                'language': language,
                'created_at': datetime.utcnow().isoformat(),
                'last_updated': datetime.utcnow().isoformat()
            }
        )
        
        # logging.info(f"Saved user profile to DB: {user_id}")  # Removed for cleaner logs
        return True
        
    except Exception as e:
        logging.error(f"Database error saving user profile: {str(e)}")
        return False

async def detect_repeat_request(query_text: str, session_context: SessionContext) -> Tuple[bool, Optional[str]]:
    """Detect if the current query is a repeat request using AI analysis"""
    try:
        # First check if we have a previous response to repeat
        if not session_context.last_response:
            # Try to get from conversation history if not in session context
            try:
                from database_operations import get_session_conversations
                conversations = await get_session_conversations(session_context.user_id, session_context.session_id, limit=1)
                if not conversations or len(conversations) == 0:
                    return False, None
            except Exception:
                return False, None
        
        # Use AI to detect if this is a repeat request
        repeat_detection_prompt = f"""
You are an AI assistant that determines if a user query is asking to repeat the previous response.

TASK:
Determine if the current query is asking the assistant to repeat, restate, or say again the previous response.

CURRENT QUERY:
"{query_text}"

CONTEXT:
- This is a BART transit assistant
- The user may be asking to repeat the previous response due to not hearing it clearly
- The user may be asking for clarification or confirmation

ANALYSIS INSTRUCTIONS:
1. Look for explicit requests to repeat, restate, or say again
2. Look for phrases indicating the user didn't hear or understand the previous response
3. Look for short queries that might be follow-ups asking for repetition
4. Consider variations in language and phrasing
5. Be sensitive to different ways users might ask for repetition

RESPONSE FORMAT:
Respond with exactly one word: "YES" if this is a repeat request, or "NO" if it's not.

Examples of repeat requests:
- "Can you repeat that?"
- "Say again"
- "What did you say?"
- "I didn't hear that"
- "Again please"
- "Repeat it"
- "Huh?"
- "What?"

Examples of NOT repeat requests:
- "What's the next train?"
- "Tell me about BART"
- "How much does it cost?"
- "What time is it?"

Analyze the query: "{query_text}"
"""

        try:
            # Import the AI function dynamically to avoid circular imports
            import sys
            import importlib
            
            main_module = sys.modules.get('main')
            if main_module is None:
                main_module = importlib.import_module('main')
            
            ai_response = await main_module.non_streaming_claude_response({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 10,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": [{"type": "text", "text": repeat_detection_prompt}]}]
            })
            
            if ai_response and isinstance(ai_response, str):
                ai_response = ai_response.strip().upper()
                if "YES" in ai_response:
                    logging.info(f"AI detected repeat request: {query_text}")
                    return True, "ai_detected_repeat"
                elif "NO" in ai_response:
                    logging.info(f"AI determined not a repeat request: {query_text}")
                    return False, None
                else:
                    logging.warning(f"AI returned unexpected response for repeat detection: {ai_response}")
            
        except Exception as ai_error:
            logging.error(f"Error calling AI for repeat detection: {str(ai_error)}")
        
        # Fallback to simple keyword detection if AI fails
        query_lower = query_text.lower().strip()
        
        # Basic fallback patterns
        fallback_patterns = [
            'repeat', 'say again', 'what did you say', 'can you repeat', 'say that again',
            'repeat that', 'repeat it', 'say it again', 'can you say that again',
            'what did you just say', 'repeat please', 'say again please',
            'can you repeat that', 'could you repeat', 'could you say again',
            'repeat the last', 'repeat your last', 'repeat your response',
            'what was that', 'what did you say again', 'repeat your answer',
            'say that one more time', 'repeat the answer', 'repeat the response',
            'can you repeat it', 'can you repeat it again', 'repeat it again',
            'say it again please', 'can you say it again', 'could you say it again',
            'i didn\'t hear it', 'i didn\'t hear', 'i didn\'t catch that', 'i didn\'t catch it',
            'i missed that', 'i missed it', 'what did you say', 'what was that again',
            'again', 'more', 'huh', 'what?', 'eh?'
        ]
        
        if any(pattern in query_lower for pattern in fallback_patterns):
            logging.info(f"Fallback keyword detection found repeat request: {query_text}")
            return True, "fallback_keyword_detection"
        
        # Check if query is very short and we have a previous response
        if len(query_text.strip()) <= 3 and session_context.last_response:
            logging.info(f"Short query detected as potential repeat request: {query_text}")
            return True, "short_query_fallback"
        
        return False, None
        
    except Exception as e:
        logging.error(f"Error detecting repeat request: {str(e)}")
        return False, None

async def handle_repeat_request(previous_query: str, session_context: SessionContext, current_query: str = None) -> Tuple[str, bool]:
    """Handle repeat request by retrieving the last response
    
    Returns:
        Tuple of (response, success_flag)
    """
    try:
        # Get the stored response from session context
        stored_response = session_context.get_stored_response()
        
        if stored_response:
            logging.info(f"Handling repeat request with stored response for query: {previous_query}")
            return stored_response, True
        
        # If no stored response in session context, try to get from conversation history
        logging.warning(f"No stored response in session context for repeat request: {current_query}")
        logging.info("Attempting to retrieve last response from conversation history...")
        
        # Get the last conversation from the database
        try:
            from database_operations import get_session_conversations
            conversations = await get_session_conversations(session_context.user_id, session_context.session_id, limit=1)
            
            if conversations and len(conversations) > 0:
                last_conversation = conversations[0]
                last_response = last_conversation.get('response', '')
                
                if last_response and last_response.strip():
                    logging.info(f"Retrieved last response from conversation history for repeat request")
                    # Store it in the session context for future use
                    session_context.store_response(last_response)
                    return last_response, True
                else:
                    logging.warning(f"Last conversation found but response is empty")
            else:
                logging.warning(f"No conversations found in database for user {session_context.user_id}")
        except Exception as db_error:
            logging.error(f"Error retrieving conversation history: {str(db_error)}")
        
        # No stored response available, send a generic message
        logging.warning(f"No stored response available for repeat request: {current_query}")
        return "I don't have a previous response to repeat. Could you please ask your question again?", False
        
    except Exception as e:
        logging.error(f"Error handling repeat request: {str(e)}")
        return "I'm sorry, I couldn't retrieve the previous response. Could you please ask your question again?", False

# Function to format a repeat response
def format_repeat_response(response: str, original_query: str = None) -> Dict[str, Any]:
    """Format a repeat response as a dictionary"""
    return {
        "type": "repeat_response",
        "response": response,
        "original_query": original_query,
        "timestamp": datetime.now().isoformat()
    }

async def delete_session(user_id: str, session_id: str) -> bool:
    """Delete a session from the database"""
    try:
        logging.info(f"Deleting session: {user_id}:{session_id}")
        success = await delete_session_from_db(user_id, session_id)
        if success:
            logging.info(f"Successfully deleted session: {user_id}:{session_id}")
        else:
            logging.error(f"Failed to delete session: {user_id}:{session_id}")
        return success
    except Exception as e:
        logging.error(f"Error deleting session: {str(e)}")
        return False

# ======================================GLOBAL INSTANCES======================================
# Initialize global database-only managers
import os
session_manager = DatabaseSessionManager(session_timeout_minutes=int(os.getenv("SESSION_TIMEOUT_MINUTES")))
# context_manager is imported from context_manager.py

# Session timeout configuration
SESSION_TIMEOUT_MINUTES = int(os.getenv("SESSION_TIMEOUT_MINUTES"))