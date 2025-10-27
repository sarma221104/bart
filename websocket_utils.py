import logging
import json
import gc
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from fastapi import WebSocket
from database_operations import (
    get_ws_state_from_db,
    update_ws_state_in_db,
    save_ws_state_to_db
)


def is_websocket_connected(websocket: WebSocket) -> bool:
    """Check if WebSocket is still connected and can receive messages"""
    try:
        if hasattr(websocket, 'client_state'):
            from starlette.websockets import WebSocketState
            # Check if WebSocket is in CONNECTED state
            # DISCONNECTED or CONNECTING states mean we can't send
            return websocket.client_state == WebSocketState.CONNECTED
        # Fallback: assume connected if we can't determine state
        return True
    except Exception as e:
        logging.debug(f"Error checking WebSocket state: {e}")
        return False


async def safe_websocket_send_text(websocket: WebSocket, message: str) -> bool:
    """Safely send text message to WebSocket with error handling"""
    try:
        if is_websocket_connected(websocket):
            await websocket.send_text(message)
            return True
        else:
            logging.info("WebSocket is disconnected, skipping message send")
            return False
    except Exception as e:
        logging.warning(f"Failed to send WebSocket message: {str(e)}")
        return False


async def safe_websocket_send_bytes(websocket: WebSocket, data: bytes) -> bool:
    """Safely send bytes message to WebSocket with error handling"""
    try:
        if is_websocket_connected(websocket):
            await websocket.send_bytes(data)
            return True
        else:
            logging.info("WebSocket is disconnected, skipping bytes send")
            return False
    except Exception as e:
        error_msg = str(e)
        # Suppress detailed keepalive timeout warnings - just log connection lost
        if "keepalive ping timeout" in error_msg or "1011" in error_msg:
            logging.info("WebSocket connection lost (keepalive timeout)")
        else:
            logging.warning(f"Failed to send WebSocket bytes: {error_msg}")
        return False


async def safe_websocket_close(websocket: WebSocket, code: int = 1000, reason: str = "Normal closure") -> bool:
    """Safely close WebSocket connection with error handling"""
    try:
        if is_websocket_connected(websocket):
            await websocket.close(code=code, reason=reason)
            return True
        else:
            logging.info("WebSocket already disconnected, skipping close")
            return False
    except Exception as e:
        logging.warning(f"Failed to close WebSocket: {str(e)}")
        return False


# ======================================DB STATE FUNCTIONS======================================

async def get_state_value_from_db(user_id: str, session_id: str, key: str, default: Any = None) -> Any:
    """Get a state value directly from DynamoDB"""
    try:
        state_data = await get_ws_state_from_db(user_id, session_id)
        return state_data.get(key, default)
    except Exception as e:
        logging.error(f"Error getting state value from DB: {str(e)}")
        return default

async def set_state_value_in_db(user_id: str, session_id: str, key: str, value: Any) -> bool:
    """Set a state value directly in DynamoDB"""
    try:
        return await update_ws_state_in_db(user_id, session_id, {key: value})
    except Exception as e:
        logging.error(f"Error setting state value in DB: {str(e)}")
        return False

async def update_state_values_in_db(user_id: str, session_id: str, updates: Dict[str, Any]) -> bool:
    """Update multiple state values directly in DynamoDB"""
    try:
        return await update_ws_state_in_db(user_id, session_id, updates)
    except Exception as e:
        logging.error(f"Error updating state values in DB: {str(e)}")
        return False

async def initialize_state_in_db(user_id: str, session_id: str, initial_values: Dict[str, Any] = None) -> bool:
    """Initialize state with default values directly in DynamoDB"""
    try:
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
            'location_response_processed': False,
            'language': 'en',
            'original_query_language': None,
            'original_query_text': None,
            'complete_query': False,
            'origin_station': None,
            'destination_station': None,
            'final_query': None,
            'original_query_for_response': None,
            'replaced_query': None
        }
        
        # Update with any provided values
        if initial_values:
            initial_state.update(initial_values)
        
        return await save_ws_state_to_db(user_id, session_id, initial_state)
    except Exception as e:
        logging.error(f"Error initializing state in DB: {str(e)}")
        return False

async def get_session_info_from_request(websocket: WebSocket) -> Tuple[str, str, str]:
    """Extract user_id, session_id and language from WebSocket connection parameters"""
    try:
        connection_params = dict(websocket.query_params)
        
        # Extract user_id and session_id from query parameters
        user_id = connection_params.get("user_id")
        session_id = connection_params.get("session_id")
        language = connection_params.get("language", "en")
        
        if not user_id or not session_id:
            logging.error("Missing user_id or session_id in WebSocket connection parameters")
            return None, None, language
        
        return user_id, session_id, language
    except Exception as e:
        logging.error(f"Error extracting session info from request: {str(e)}")
        return None, None, "en"

async def get_or_create_session_info(websocket: WebSocket) -> Tuple[str, str, str, bool]:
    """Get or create session information from WebSocket
    
    Returns:
        Tuple[str, str, str, bool]: (user_id, session_id, language, is_new_session)
    """
    try:
        # Try to get from query parameters first
        user_id, session_id, language = await get_session_info_from_request(websocket)
        is_new_session = False
        
        # If session_id is missing, create a new one
        if not session_id and user_id:
            import uuid
            from session_manager import create_session
            
            session_id = str(uuid.uuid4())
            is_new_session = True
            
            # Create new session in database
            try:
                session_created = await create_session(
                    user_id=user_id,
                    session_id=session_id,
                    language=language
                )
                
                if not session_created:
                    logging.warning(f"Failed to create session for user {user_id}, but continuing anyway")
            except Exception as e:
                logging.warning(f"Error creating session for user {user_id}: {str(e)}, but continuing anyway")
        
        return user_id, session_id, language, is_new_session
    except Exception as e:
        logging.error(f"Error in get_or_create_session_info: {str(e)}")
        return None, None, "en", False

async def get_state_for_response(user_id: str, session_id: str) -> Dict[str, Any]:
    """Get state values needed for response handling
    
    Returns:
        Dict[str, Any]: Dictionary with state values
    """
    try:
        state = {}
        keys = [
            'response_sent', 'last_saved_conversation', 'skip_current_response',
            'awaiting_disambiguation', 'awaiting_station_validation', 'awaiting_location',
            'final_query', 'is_station_replacement', 'replaced_query',
            'original_query_language', 'original_query_text'
        ]
        
        # Get all state values in one call to reduce DynamoDB operations
        db_state = await get_ws_state_from_db(user_id, session_id)
        
        for key in keys:
            state[key] = db_state.get(key, None)
            
        # Convert boolean flags to actual booleans
        boolean_keys = [
            'response_sent', 'skip_current_response', 'awaiting_disambiguation',
            'awaiting_station_validation', 'awaiting_location', 'is_station_replacement'
        ]
        
        for key in boolean_keys:
            if key in state:
                state[key] = bool(state[key])
            else:
                state[key] = False
                
        return state
    except Exception as e:
        logging.error(f"Error getting state for response: {str(e)}")
        return {}

async def get_station_state(user_id: str, session_id: str) -> Dict[str, Any]:
    """Get station-related state values
    
    Returns:
        Dict[str, Any]: Dictionary with station state values
    """
    try:
        state = {}
        keys = [
            'origin_station', 'destination_station', 'ambiguous_station',
            'station_options', 'invalid_station', 'station_suggestions',
            'complete_query', 'original_query'
        ]
        
        # Get all state values in one call to reduce DynamoDB operations
        db_state = await get_ws_state_from_db(user_id, session_id)
        
        for key in keys:
            state[key] = db_state.get(key, None)
            
        # Convert boolean flags to actual booleans
        if 'complete_query' in state:
            state['complete_query'] = bool(state['complete_query'])
        else:
            state['complete_query'] = False
                
        return state
    except Exception as e:
        logging.error(f"Error getting station state: {str(e)}")
        return {}

async def get_location_state(user_id: str, session_id: str) -> Dict[str, Any]:
    """Get location-related state values
    
    Returns:
        Dict[str, Any]: Dictionary with location state values
    """
    try:
        state = {}
        keys = [
            'awaiting_location', 'destination_station', 'nearest_station',
            'user_location', 'location_response_processed'
        ]
        
        # Get all state values in one call to reduce DynamoDB operations
        db_state = await get_ws_state_from_db(user_id, session_id)
        
        for key in keys:
            state[key] = db_state.get(key, None)
            
        # Convert boolean flags to actual booleans
        boolean_keys = ['awaiting_location', 'location_response_processed']
        
        for key in boolean_keys:
            if key in state:
                state[key] = bool(state[key])
            else:
                state[key] = False
                
        return state
    except Exception as e:
        logging.error(f"Error getting location state: {str(e)}")
        return {}

async def cleanup_websocket_resources(user_id: str, session_id: str, audio_buffer=None, pre_buffer=None):
    """Production-grade cleanup of WebSocket resources to prevent memory leaks"""
    try:
        logging.info(f"Starting comprehensive cleanup for user {user_id}, session {session_id}")
        
        # Clear managed audio buffer with timeout protection
        if audio_buffer is not None:
            try:
                # Use asyncio.wait_for to prevent hanging on cleanup
                await asyncio.wait_for(asyncio.to_thread(_cleanup_audio_buffer, audio_buffer), timeout=5.0)
                logging.info("Managed audio buffer closed")
            except asyncio.TimeoutError:
                logging.warning("Audio buffer cleanup timed out")
            except Exception as e:
                logging.warning(f"Error cleaning audio buffer: {str(e)}")
        
        # Clear pre-buffer (deque) with timeout protection
        if pre_buffer is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(_cleanup_pre_buffer, pre_buffer), timeout=2.0)
                logging.info("Pre-buffer cleared")
            except asyncio.TimeoutError:
                logging.warning("Pre-buffer cleanup timed out")
            except Exception as e:
                logging.warning(f"Error clearing pre-buffer: {str(e)}")
        
        # CRITICAL FIX: DO NOT clean up session state or context data on disconnect
        # Session context should persist for user to continue conversations
        # Only clean up WebSocket-specific resources, not database context
        logging.info("Skipping session state and context cleanup - preserving for user continuity")
        
        # Force garbage collection with timeout protection
        try:
            await asyncio.wait_for(asyncio.to_thread(_force_gc), timeout=3.0)
            logging.info("Garbage collection performed")
        except asyncio.TimeoutError:
            logging.warning("Garbage collection timed out")
        except Exception as e:
            logging.warning(f"Error during garbage collection: {str(e)}")
        
        # Clean up audio resources with timeout protection
        try:
            await asyncio.wait_for(asyncio.to_thread(_cleanup_audio_resources), timeout=5.0)
            logging.info("Audio resources cleaned up")
        except asyncio.TimeoutError:
            logging.warning("Audio resources cleanup timed out")
        except Exception as e:
            logging.warning(f"Error cleaning audio resources: {str(e)}")
        
        logging.info(f"Comprehensive WebSocket cleanup completed for user {user_id}, session {session_id}")
        
    except Exception as e:
        logging.error(f"Error during comprehensive WebSocket resource cleanup: {str(e)}")

def _cleanup_audio_buffer(audio_buffer):
    """Helper function for audio buffer cleanup"""
    if hasattr(audio_buffer, 'close'):
        audio_buffer.close()
    elif hasattr(audio_buffer, 'clear'):
        audio_buffer.clear()

def _cleanup_pre_buffer(pre_buffer):
    """Helper function for pre-buffer cleanup"""
    if hasattr(pre_buffer, 'clear'):
        pre_buffer.clear()

async def _cleanup_session_state(user_id: str, session_id: str):
    """Helper function for session state cleanup"""
    cleanup_state = {
        'response_sent': False,
        'last_saved_conversation': None,
        'skip_current_response': False,
        'awaiting_disambiguation': False,
        'awaiting_station_validation': False,
        'awaiting_location': False,
        'invalid_station': None,
        'station_suggestions': None,
        'original_query': None,
        'ambiguous_station': None,
        'station_options': None,
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
        'replaced_query': None,
        'nearest_station': None,
        'user_location': None,
        'cleanup_timestamp': datetime.utcnow().isoformat()
    }
    await update_ws_state_in_db(user_id, session_id, cleanup_state)

async def _cleanup_context_data(user_id: str, session_id: str):
    """Helper function for context data cleanup"""
    from context_manager import context_manager
    await context_manager.cleanup_session_context(user_id, session_id)

def _force_gc():
    """Helper function for garbage collection"""
    import gc
    collected = gc.collect()
    logging.info(f"Garbage collection performed: {collected} objects collected")

def _cleanup_audio_resources():
    """Helper function for audio resources cleanup"""
    from audio_buffer_manager import cleanup_audio_resources
    cleanup_audio_resources()
