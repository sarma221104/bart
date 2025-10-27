import logging
import uuid
import boto3
import pytz
import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from fastapi import HTTPException
from botocore.exceptions import ClientError
from decimal import Decimal

# DynamoDB setup with connection pooling
from aws_client_manager import get_dynamodb_resource

import os
dynamodb = get_dynamodb_resource()
bart_table = dynamodb.Table(os.getenv("DYNAMODB_TABLE"))

# Retry decorator for database operations
def retry_dynamodb_operation(max_retries: int = 3, delay: float = 1.0):
    """Decorator to retry DynamoDB operations on transient failures"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except ClientError as e:
                    error_info = handle_dynamodb_error(func.__name__, e)
                    if not error_info.get("retry", False):
                        raise e
                    
                    last_error = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay * (2 ** attempt))  # Exponential backoff
                        logging.warning(f"Retrying {func.__name__} (attempt {attempt + 1}/{max_retries})")
                    else:
                        logging.error(f"Max retries exceeded for {func.__name__}")
                        raise e
                except Exception as e:
                    logging.error(f"Non-retryable error in {func.__name__}: {str(e)}")
                    raise e
            
            if last_error:
                raise last_error
        return wrapper
    return decorator

def handle_dynamodb_error(operation_name: str, error: Exception):
    """Handle DynamoDB errors gracefully with retry logic"""
    error_str = str(error)
    
    if "AccessDeniedException" in error_str:
        logging.error(f"Access denied for {operation_name}. Please check IAM permissions.")
        return {"error": "Access denied", "retry": False}
    elif "ResourceNotFoundException" in error_str:
        logging.error(f"Table not found for {operation_name}. Please create the table first.")
        return {"error": "Table not found", "retry": False}
    elif "ThrottlingException" in error_str or "ProvisionedThroughputExceededException" in error_str:
        logging.warning(f"Throttling detected for {operation_name}. Retrying...")
        return {"error": "Throttling", "retry": True}
    elif "ServiceUnavailable" in error_str or "InternalServerError" in error_str:
        logging.warning(f"Service unavailable for {operation_name}. Retrying...")
        return {"error": "Service unavailable", "retry": True}
    else:
        logging.error(f"Error in {operation_name}: {error_str}")
        return {"error": error_str, "retry": False}

async def get_user_session_from_websocket(websocket) -> Tuple[Optional[str], Optional[str]]:
    """
    Production-grade function to get user_id and session_id from WebSocket connection.
    OPTIMIZED: Added timeout protection to prevent blocking
    
    Args:
        websocket: The WebSocket connection object
        
    Returns:
        Tuple[Optional[str], Optional[str]]: (user_id, session_id) or (None, None) if not found
    """
    try:
        # Generate a consistent WebSocket identifier using the WebSocket object ID
        websocket_id = f"ws_{id(websocket)}"
        
        # Look up the WebSocket connection in the ws_state table with timeout
        try:
            # Wrap the scan operation with timeout protection
            async def perform_scan():
                return await asyncio.to_thread(
                    lambda: bart_table.scan(
                        FilterExpression='record_type = :record_type',
                        ExpressionAttributeValues={
                            ':record_type': f'ws_state/{websocket_id}'
                        },
                        Limit=1  # Only need first match - reduces cost
                    )
                )
            
            # Set timeout to 3 seconds - prevent long-running scans
            response = await asyncio.wait_for(perform_scan(), timeout=3.0)
            
            if 'Items' in response and response['Items']:
                item = response['Items'][0]  # Get the first match
                user_id = item.get('user_id')
                session_id = item.get('session_id')
                
                if user_id and session_id:
                    logging.info(f"Found user_id: {user_id}, session_id: {session_id} for WebSocket {websocket_id}")
                    return user_id, session_id
                else:
                    logging.warning(f"WebSocket {websocket_id} found but missing user_id or session_id")
                    return None, None
            else:
                logging.warning(f"No WebSocket connection found for {websocket_id}")
                return None, None
        
        except asyncio.TimeoutError:
            logging.error(f"Timeout looking up WebSocket connection {websocket_id}")
            return None, None
        except Exception as e:
            logging.error(f"Error looking up WebSocket connection {websocket_id}: {str(e)}")
            return None, None
            
    except Exception as e:
        logging.error(f"Error in get_user_session_from_websocket: {str(e)}")
        return None, None

async def save_websocket_connection_mapping(websocket_id: str, user_id: str, session_id: str) -> bool:
    """
    Save WebSocket connection mapping to database for production-grade concurrent access.
    
    Args:
        websocket_id: Unique identifier for the WebSocket connection
        user_id: User ID associated with the connection
        session_id: Session ID associated with the connection
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Save WebSocket connection mapping with TTL (24 hours)
        ttl = int(time.time()) + (1440 * 60)  # 24 hours from now
        
        bart_table.put_item(
            Item={
                'user_id': user_id,
                'record_type': f'ws_state/{websocket_id}',
                'session_id': session_id,
                'created_at': datetime.now(pytz.timezone('US/Pacific')).isoformat(),
                'ttl': ttl
            }
        )
        
        logging.info(f"Saved WebSocket connection mapping: {websocket_id} -> {user_id}/{session_id}")
        return True
        
    except Exception as e:
        logging.error(f"Error saving WebSocket connection mapping: {str(e)}")
        return False

async def save_conversation(user_id: str, query: str, response: str, audio_duration: float = None, session_id: str = None, conversation_id: str = None):
    """Save a conversation record in the conversations folder with chat history."""
    try:
        print("\n========== SAVING CONVERSATION TO DB ==========")
        print(f"User ID: {user_id}")
        print(f"Session ID: {session_id}")
        print(f"Conversation ID: {conversation_id}")
        print(f"Query length: {len(query)} chars")
        print(f"Response length: {len(response)} chars")
        print("=============================================\n")
        
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        
        if not session_id:
            raise ValueError("No session_id provided")
        
        # Generate a unique chat timestamp
        chat_timestamp = datetime.now(pytz.timezone('US/Pacific')).strftime("%Y%m%d_%H%M%S")
        
        final_response = response
        if "Final Combined Response:" in response:
            final_response = response.split("Final Combined Response:", 1)[1].strip()
            if "--------" in final_response:
                final_response = final_response.split("--------", 1)[1].strip()
        
        # Store conversations in conversations folder with user_id/session_id structure
        item = {
            'user_id': user_id,
            'record_type': f'conversations/{user_id}/{session_id}/{chat_timestamp}',
            'conversation_id': conversation_id,
            'timestamp': timestamp,
            'query': query,
            'response': final_response,
            'audio_duration': audio_duration,
            'feedback': None,
            'session_id': session_id
        }
        
        try:
            bart_table.put_item(Item=item)
            logging.info(f"Saved conversation for user {user_id} in session {session_id}, conversation {conversation_id} (no TTL - permanent)")
            return conversation_id
        except Exception as table_error:
            return handle_dynamodb_error("save_conversation", table_error)
            
    except Exception as e:
        return handle_dynamodb_error("save_conversation", e)

async def get_user_conversations(user_id: str, limit: int = 10):
    """Get conversations for a user from the conversations folder."""
    try:
        print("\n========== FETCHING CONVERSATION HISTORY ==========")
        print(f"User ID: {user_id}")
        print(f"Limit: {limit}")
        print("==================================================\n")
        
        response = bart_table.query(
            KeyConditionExpression='user_id = :uid AND begins_with(record_type, :conversations)',
            ExpressionAttributeValues={
                ':uid': user_id,
                ':conversations': f'conversations/{user_id}/'
            },
            ScanIndexForward=False,
            Limit=limit
        )
        return response.get('Items', [])
    except Exception as e:
        handle_dynamodb_error("get_user_conversations", e)
        return []

async def get_session_conversations(user_id: str, session_id: str, limit: int = 10):
    """Get conversations for a specific session."""
    try:
        print(f"\n========== FETCHING SESSION CONVERSATIONS ==========")
        print(f"User ID: {user_id}")
        print(f"Session ID: {session_id}")
        print(f"Limit: {limit}")
        print("==================================================\n")
        
        response = bart_table.query(
            KeyConditionExpression='user_id = :uid AND begins_with(record_type, :session_conversations)',
            ExpressionAttributeValues={
                ':uid': user_id,
                ':session_conversations': f'conversations/{user_id}/{session_id}/'
            },
            ScanIndexForward=False,
            Limit=limit
        )
        return response.get('Items', [])
    except Exception as e:
        handle_dynamodb_error("get_session_conversations", e)
        return []

async def create_or_update_user(user_id: str):
    """Create or update a user record in the profiles folder."""
    try:
        timestamp = datetime.utcnow().isoformat()
        
        profile_item = {
            'user_id': user_id,
            'record_type': f'profiles/{user_id}',
            'last_active': timestamp
        }
        
        logging.info(f"Creating/updating user profile for {user_id}")
            
        try:
            bart_table.put_item(Item=profile_item)
            logging.info(f"Successfully updated user profile for {user_id}")
            return True
        except Exception as table_error:
            logging.error(f"Failed to update user profile: {str(table_error)}")
            return handle_dynamodb_error("create_or_update_user", table_error)
            
    except Exception as e:
        logging.error(f"Error in create_or_update_user: {str(e)}")
        return handle_dynamodb_error("create_or_update_user", e)

async def get_user_profile_from_db(user_id: str) -> Optional[Dict[str, Any]]:
    """Get user profile from database"""
    try:
        response = bart_table.get_item(
            Key={
                'user_id': user_id,
                'record_type': f'profiles/{user_id}'
            }
        )
        
        if 'Item' in response:
            return response['Item']
        return None
    except Exception as e:
        logging.error(f"Error getting user profile: {str(e)}")
        return None

async def update_user_profile_in_db(user_id: str, language: str = None) -> bool:
    """Update user profile in database"""
    try:
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        
        update_expression = 'SET last_updated = :lu'
        expression_values = {':lu': timestamp}
        
        if language is not None:
            update_expression += ', language = :lang'
            expression_values[':lang'] = language
        
        bart_table.update_item(
            Key={
                'user_id': user_id,
                'record_type': f'profiles/{user_id}'
            },
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values
        )
        logging.info(f"Updated user profile for {user_id}")
        return True
    except Exception as e:
        logging.error(f"Error updating user profile: {str(e)}")
        return False

async def save_session_info(user_id: str, session_id: str, language: str = 'en'):
    """Save session information with TTL for automatic expiration."""
    try:
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        
        # Calculate TTL for 24 hours from now using PDT (consistent with validation)
        ttl_timestamp = int((datetime.now(pytz.timezone('US/Pacific')) + timedelta(minutes=1440)).timestamp())
        
        session_item = {
            'user_id': user_id,
            'record_type': f'sessions/{user_id}/{session_id}',
            'session_id': session_id,
            'language': language,
            'created_at': timestamp,
            'last_active': timestamp,
            'is_active': True,
            'ttl': ttl_timestamp  # TTL for session expiration only
        }
        
        logging.info(f"Saving session info for user {user_id}, session {session_id} with TTL {ttl_timestamp}")
            
        try:
            bart_table.put_item(Item=session_item)
            logging.info(f"Successfully saved session info for {user_id}, session {session_id}")
            return True
        except Exception as table_error:
            logging.error(f"Failed to save session info: {str(table_error)}")
            return handle_dynamodb_error("save_session_info", table_error)
            
    except Exception as e:
        logging.error(f"Error in save_session_info: {str(e)}")
        return handle_dynamodb_error("save_session_info", e)

async def update_session_activity(user_id: str, session_id: str):
    """
    Update session activity timestamp in the database.
    
    Behavior:
    - If session is valid: Update last_active timestamp, don't extend TTL
    - If session not found or expired: Return False
    
    This ensures sessions expire after exactly 24 hours from creation,
    regardless of activity.
    """
    try:
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        current_time = int(datetime.now(pytz.timezone('US/Pacific')).timestamp())  # Use PDT for consistency
        
        # Check if the current session exists and hasn't expired
        try:
            response = bart_table.get_item(
                Key={
                    'user_id': user_id,
                    'record_type': f'sessions/{user_id}/{session_id}'
                }
            )
            
            if 'Item' not in response:
                logging.warning(f"Session {session_id} for user {user_id} not found")
                return False
                
            session_item = response['Item']
            
            # Check if session has expired based on TTL
            if 'ttl' in session_item and session_item['ttl'] < current_time:
                logging.info(f"Session {session_id} for user {user_id} has expired (TTL: {session_item['ttl']}, current: {current_time})")
                return False
            
            # Session hasn't expired - only update last_active, don't extend TTL
            try:
                bart_table.update_item(
                    Key={
                        'user_id': user_id,
                        'record_type': f'sessions/{user_id}/{session_id}'
                    },
                    UpdateExpression='SET last_active = :la',
                    ExpressionAttributeValues={
                        ':la': timestamp
                    }
                )
                # logging.info(f"Updated session activity for user {user_id}, session {session_id} (TTL unchanged)")  # Removed for cleaner logs
                return True
            except Exception as table_error:
                logging.error(f"Failed to update session activity: {str(table_error)}")
                return handle_dynamodb_error("update_session_activity", table_error)
                
        except Exception as get_error:
            logging.error(f"Failed to get session for TTL check: {str(get_error)}")
            # If we can't check TTL, just update activity without extending TTL
            try:
                bart_table.update_item(
                    Key={
                        'user_id': user_id,
                        'record_type': f'sessions/{user_id}/{session_id}'
                    },
                    UpdateExpression='SET last_active = :la',
                    ExpressionAttributeValues={
                        ':la': timestamp
                    }
                )
                # logging.info(f"Updated session activity for user {user_id}, session {session_id} (TTL check failed)")  # Removed for cleaner logs
                return True
            except Exception as table_error:
                logging.error(f"Failed to update session activity: {str(table_error)}")
                return handle_dynamodb_error("update_session_activity", table_error)
            
    except Exception as e:
        logging.error(f"Error in update_session_activity: {str(e)}")
        return handle_dynamodb_error("update_session_activity", e)

async def save_conversation_feedback(conversation_id: str, feedback: dict):
    """Save user feedback for a conversation in the users folder."""
    try:
        valid_feedback_values = ['excellent', 'helpful', 'okay', 'needs_improvement', 'not_helpful']
        feedback_value = feedback.get('feedback')
        
        if not feedback_value or feedback_value not in valid_feedback_values:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid feedback value. Must be one of: {', '.join(valid_feedback_values)}"
            )
        
        response = bart_table.scan(
            FilterExpression='conversation_id = :cid',
            ExpressionAttributeValues={':cid': conversation_id}
        )
        items = response.get('Items', [])
        
        if not items:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        conversation = items[0]
        
        bart_table.update_item(
            Key={
                'user_id': conversation['user_id'],
                'record_type': conversation['record_type']
            },
            UpdateExpression='SET feedback = :fb',
            ExpressionAttributeValues={
                ':fb': feedback_value
            }
        )
        
        return {"message": "Feedback saved successfully"}
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def get_previous_conversations(user_id: str, session_id: str, limit: int = 5) -> list:
    """Get the last 5 conversations from the same user for context."""
    try:
        print("\n========== FETCHING CONVERSATION HISTORY ==========")
        print(f"User ID: {user_id}")
        print(f"Session ID: {session_id}")
        print(f"Limit: {limit}")
        print("==================================================\n")
        
        response = bart_table.query(
            KeyConditionExpression='user_id = :uid AND begins_with(record_type, :conv_prefix)',
            ExpressionAttributeValues={
                ':uid': user_id,
                ':conv_prefix': f'users/{user_id}/conversations/'
            },
            ScanIndexForward=False,  
            Limit=limit
        )
        conversations = response.get('Items', [])
        
        if conversations:
            sorted_convs = sorted(conversations, key=lambda x: x.get('timestamp', ''), reverse=True)
            for idx, conv in enumerate(sorted_convs, 1):
                timestamp_str = conv.get('timestamp', '')
        else:
            print("No previous conversations found")
        print("=========================================\n")
        
        return conversations
    except Exception as e:
        logging.error(f"Error getting previous conversations: {str(e)}")
        return []

async def format_previous_conversations(conversations: list) -> str:
    """Format previous conversations for context."""
    if not conversations:
        return "No previous context available."
    
    sorted_convs = sorted(conversations, key=lambda x: x.get('timestamp', ''))
    
    context = ["IMPORTANT: Previous conversations are ONLY for understanding context. For API-related queries (schedules, train times, etc.), ALWAYS use current API data and IGNORE previous answers completely. NEVER use station names, routes, or schedules from previous conversations in your current response."]
    for conv in sorted_convs:
        timestamp_str = conv.get('timestamp', '')
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            timestamp_pdt = pytz.timezone('US/Pacific').localize(timestamp)
            formatted_timestamp = timestamp_pdt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
        except Exception as e:
            formatted_timestamp = timestamp_str
        
        query = conv.get('query', '')
        response = conv.get('response', '')
        
        context.append(f"Previous Q ({formatted_timestamp}): {query}")
        context.append(f"Previous A (POTENTIALLY OUTDATED): {response}\n")
    
    context.append("CRITICAL REMINDER: For any API-related questions about train times, schedules, or real-time information, ONLY use the current API data in your response. Previous answers contain outdated information and MUST NOT influence your current answer content. NEVER use station names, routes, or schedules from previous conversations - ONLY use what's in the current API response.")
    
    return "\n".join(context)

# ======================================CONTEXT DATABASE FUNCTIONS======================================
async def save_context_to_db(user_id: str, session_id: str, context_data: Dict[str, Any]) -> bool:
    """Save session context to DynamoDB"""
    try:
        # Add timestamp and TTL
        context_data['timestamp'] = datetime.utcnow().isoformat()
        context_data['ttl'] = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        
        bart_table.put_item(
            Item={
                'user_id': user_id,
                'record_type': f'contexts/{user_id}/{session_id}',
                'session_id': session_id,
                **context_data
            }
        )
        
        # logging.info(f"Saved context to DB: {user_id}:{session_id}")  # Removed for cleaner logs
        return True
        
    except Exception as e:
        logging.error(f"Database error saving context: {str(e)}")
        return handle_dynamodb_error("save_context_to_db", e)

async def get_context_from_db(user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Get session context from DynamoDB"""
    try:
        response = bart_table.get_item(
            Key={
                'user_id': user_id,
                'record_type': f'contexts/{user_id}/{session_id}'
            }
        )
        
        if 'Item' in response:
            item = response['Item']
            logging.info(f"Retrieved context from DB: {user_id}:{session_id}")
            return item
        else:
            logging.info(f"Context not found in DB: {user_id}:{session_id}")
            return None
            
    except Exception as e:
        logging.error(f"Database error getting context: {str(e)}")
        return None

async def update_context_in_db(user_id: str, session_id: str, context_data: Dict[str, Any]) -> bool:
    """Update session context in DynamoDB"""
    try:
        # Prepare update expression
        update_expression = "SET "
        expression_values = {}
        
        for key, value in context_data.items():
            if key not in ['user_id', 'record_type', 'session_id']:
                update_expression += f"#{key} = :{key}, "
                expression_values[f':{key}'] = value
        
        # Remove trailing comma and space
        update_expression = update_expression.rstrip(', ')
        
        # Add timestamp update
        update_expression += ", #timestamp = :timestamp"
        expression_values[':timestamp'] = datetime.utcnow().isoformat()
        
        # Create expression attribute names
        expression_names = {f'#{key}': key for key in context_data.keys() if key not in ['user_id', 'record_type', 'session_id']}
        expression_names['#timestamp'] = 'timestamp'
        
        bart_table.update_item(
            Key={
                'user_id': user_id,
                'record_type': f'contexts/{user_id}/{session_id}'
            },
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values
        )
        
        logging.info(f"Updated context in DB: {user_id}:{session_id}")
        return True
        
    except Exception as e:
        logging.error(f"Database error updating context: {str(e)}")
        return handle_dynamodb_error("update_context_in_db", e)

async def delete_context_from_db(user_id: str, session_id: str) -> bool:
    """Delete session context from DynamoDB"""
    try:
        bart_table.delete_item(
            Key={
                'user_id': user_id,
                'record_type': f'contexts/{user_id}/{session_id}'
            }
        )
        
        logging.info(f"Deleted context from DB: {user_id}:{session_id}")
        return True
        
    except Exception as e:
        logging.error(f"Database error deleting context: {str(e)}")
        return handle_dynamodb_error("delete_context_from_db", e)

async def cleanup_expired_contexts_from_db() -> int:
    """Clean up expired context records from database"""
    try:
        # This function would typically scan for expired records and delete them
        # For now, we'll just return 0 as DynamoDB TTL handles expiration automatically
        # logging.info("Context cleanup not needed - DynamoDB TTL handles expiration")  # Removed for cleaner logs
        return 0
        
    except Exception as e:
        logging.error(f"Error in context cleanup: {str(e)}")
        return 0

# WebSocket state related functions

async def save_ws_state_to_db(user_id: str, session_id: str, state_data: Dict[str, Any]) -> bool:
    """Save WebSocket state data to DynamoDB"""
    try:
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        
        # Create a copy of state_data to avoid modifying the input
        state_item = state_data.copy()
        
        # Add required fields
        state_item['user_id'] = user_id
        state_item['record_type'] = f'ws_state/{user_id}/{session_id}'
        state_item['timestamp'] = timestamp
        state_item['session_id'] = session_id
        
        # Remove any functions or non-serializable objects
        for key in list(state_item.keys()):
            if callable(state_item[key]) or key == 'websocket':
                state_item.pop(key, None)
        
        # Convert float values to Decimal for DynamoDB compatibility
        for key, value in state_item.items():
            if isinstance(value, float):
                state_item[key] = Decimal(str(value))
        
        # Save to DynamoDB
        bart_table.put_item(Item=state_item)
        
        # logging.info(f"Saved WebSocket state to DB: {user_id}:{session_id}")  # Removed for cleaner logs
        return True
        
    except Exception as e:
        logging.error(f"Database error saving WebSocket state: {str(e)}")
        return handle_dynamodb_error("save_ws_state_to_db", e)

async def get_ws_state_from_db(user_id: str, session_id: str) -> Dict[str, Any]:
    """Get WebSocket state data from DynamoDB"""
    try:
        response = bart_table.get_item(
            Key={
                'user_id': user_id,
                'record_type': f'ws_state/{user_id}/{session_id}'
            }
        )
        
        if 'Item' in response:
            # Return the item but exclude the metadata fields
            state_data = response['Item']
            metadata_fields = ['user_id', 'record_type', 'timestamp']
            return {k: v for k, v in state_data.items() if k not in metadata_fields}
        
        # Return empty dict if no state found
        return {}
        
    except Exception as e:
        logging.error(f"Database error getting WebSocket state: {str(e)}")
        return {}

async def update_ws_state_in_db(user_id: str, session_id: str, state_updates: Dict[str, Any]) -> bool:
    """Update specific WebSocket state properties in DynamoDB"""
    try:
        # Get existing state
        current_state = await get_ws_state_from_db(user_id, session_id)
        
        # Merge updates into current state
        current_state.update(state_updates)
        
        # Save the updated state
        return await save_ws_state_to_db(user_id, session_id, current_state)
        
    except Exception as e:
        logging.error(f"Database error updating WebSocket state: {str(e)}")
        return handle_dynamodb_error("update_ws_state_in_db", e)

# Note: delete_ws_state_from_db function removed as it's not used
# State cleanup is handled by TTL in DynamoDB

# ======================================ERROR LOGGING FUNCTIONS======================================
@retry_dynamodb_operation(max_retries=3, delay=1.0)
async def save_error_log_to_db(
    user_id: str, 
    session_id: str, 
    error_message: str, 
    error_type: str = "general",
    query_text: str = None,
    stack_trace: str = None
) -> bool:
    """
    Save error logs to DynamoDB for monitoring and debugging
    
    Args:
        user_id: User identifier
        session_id: Session identifier
        error_message: The error message
        error_type: Type of error (e.g., 'query_processing', 'websocket', 'database', 'general')
        query_text: The query that caused the error (if applicable)
        stack_trace: Full stack trace (if available)
    
    Returns:
        bool: True if saved successfully, False otherwise
    """
    try:
        error_id = str(uuid.uuid4())
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        
        error_item = {
            'user_id': user_id,
            'record_type': f'error_log/{user_id}/{session_id}/{error_id}',
            'error_id': error_id,
            'session_id': session_id,
            'timestamp': timestamp,
            'error_message': error_message,
            'error_type': error_type,
            'ttl': int((datetime.utcnow() + timedelta(days=30)).timestamp())  # Keep errors for 30 days
        }
        
        # Add optional fields if provided
        if query_text:
            error_item['query_text'] = query_text
        if stack_trace:
            # Truncate stack trace if too long (DynamoDB item size limit)
            error_item['stack_trace'] = stack_trace[:5000] if len(stack_trace) > 5000 else stack_trace
        
        # Save to DynamoDB
        bart_table.put_item(Item=error_item)
        
        logging.info(f"Saved error log to DB: {user_id}:{session_id}:{error_id}")
        return True
        
    except Exception as e:
        logging.error(f"Failed to save error log to database: {str(e)}")
        # Don't raise exception - we don't want error logging to break the application
        return False

async def get_error_logs_for_session(user_id: str, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Retrieve error logs for a specific session
    
    Args:
        user_id: User identifier
        session_id: Session identifier
        limit: Maximum number of error logs to retrieve
    
    Returns:
        List of error log items
    """
    try:
        response = bart_table.query(
            KeyConditionExpression='user_id = :uid AND begins_with(record_type, :record_type)',
            ExpressionAttributeValues={
                ':uid': user_id,
                ':record_type': f'error_log/{user_id}/{session_id}/'
            },
            ScanIndexForward=False,  # Most recent first
            Limit=limit
        )
        
        return response.get('Items', [])
        
    except Exception as e:
        logging.error(f"Failed to retrieve error logs: {str(e)}")
        return []
