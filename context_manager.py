"""
Production-Grade Context Management System
Consolidates all context continuity functionality into a single, optimized module.

ARCHITECTURE DECISIONS:
- No in-memory caching: Direct DynamoDB access for better scalability
- DynamoDB provides sub-10ms latency, making caching unnecessary
- Eliminates memory management complexity and potential memory leaks
- Supports unlimited concurrent users without memory constraints
- Consistent data across all application instances
"""

import logging
import re
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, Tuple, Optional, List
from models import SessionContext, QueryContext
from database_operations import (
    get_context_from_db, delete_context_from_db, cleanup_expired_contexts_from_db,
    save_context_to_db, get_session_conversations
)
class ProductionContextManager:
    """
    Production-grade context manager with optimized performance.
    Consolidates all context continuity functionality.
    """
    
    def __init__(self):
        logging.info("ProductionContextManager initialized - enterprise-grade context management")
        # No caching - direct DynamoDB access for production scalability
        # DynamoDB provides sub-10ms latency, making caching unnecessary
    
    # ======================================CORE CONTEXT MANAGEMENT======================================
    
    async def get_session_context(self, user_id: str, session_id: str) -> SessionContext:
        """Get or create session context from database with structured data"""
        try:
            # Direct DynamoDB access - no caching for production scalability
            # DynamoDB provides sub-10ms latency, making in-memory caching unnecessary
            context_data = await get_context_from_db(user_id, session_id)
            
            if context_data:
                session_context = self._reconstruct_session_context(user_id, session_id, context_data)
                logging.info(f"Retrieved context from DynamoDB for {user_id}:{session_id}")
                return session_context
            else:
                # Create new context
                session_context = SessionContext(user_id=user_id, session_id=session_id)
                logging.info(f"Created new context for {user_id}:{session_id}")
                return session_context
                
        except Exception as e:
            logging.error(f"Error getting session context: {str(e)}")
            return SessionContext(user_id=user_id, session_id=session_id)
    
    def _reconstruct_session_context(self, user_id: str, session_id: str, context_data: Dict[str, Any]) -> SessionContext:
        """Reconstruct SessionContext from database data"""
        session_context = SessionContext(user_id=user_id, session_id=session_id)
        
        # Handle new structured format
        if 'current_query' in context_data:
            current_query = context_data['current_query']
            session_context.last_intent = current_query.get('intent')
            session_context.last_category = current_query.get('category')
            session_context.last_api_endpoint = current_query.get('api_endpoint')
            session_context.cached_parameters = current_query.get('parameters', {})
            
            logging.info(f"Retrieved structured context - Intent: {session_context.last_intent}, "
                        f"Category: {session_context.last_category}, "
                        f"API Endpoint: {session_context.last_api_endpoint}")
        
        # Handle legacy format
        else:
            if 'cached_parameters' in context_data:
                session_context.cached_parameters = context_data['cached_parameters']
            session_context.last_intent = context_data.get('last_intent')
            session_context.last_category = context_data.get('last_category')
            session_context.last_api_endpoint = context_data.get('last_api_endpoint')
        
        # Restore recent contexts
        recent_contexts_data = context_data.get('query_history', context_data.get('recent_contexts', []))
        recent_contexts_data = recent_contexts_data[-5:]  # Keep only last 5
        session_context.recent_contexts = []
        
        for ctx_data in recent_contexts_data:
            if ctx_data:
                query_context = QueryContext(
                    intent=ctx_data.get('intent', ''),
                    category=ctx_data.get('category', ''),
                    api_endpoint=ctx_data.get('api_endpoint'),
                    parameters=ctx_data.get('parameters', {}),
                    timestamp=datetime.fromisoformat(ctx_data.get('timestamp', datetime.utcnow().isoformat())),
                    query_text=ctx_data.get('query_text', ''),
                    response_preview=ctx_data.get('response_preview', '')
                )
                session_context.recent_contexts.append(query_context)
        
        # Restore other fields
        if session_context.recent_contexts:
            session_context.last_query_context = session_context.recent_contexts[-1]
        
        session_context.conversation_count = context_data.get('conversation_count', 0)
        session_context.last_activity = datetime.fromisoformat(context_data.get('last_activity', datetime.utcnow().isoformat()))
        session_context.last_response = context_data.get('last_response')
        if context_data.get('last_response_timestamp'):
            session_context.last_response_timestamp = datetime.fromisoformat(context_data['last_response_timestamp'])
        
        return session_context
    
    async def add_query_context(self, user_id: str, session_id: str, query_context: QueryContext):
        """Add query context to session and save to database"""
        try:
            session_context = await self.get_session_context(user_id, session_id)
            session_context.add_context(query_context)
            session_context.update_cached_parameters(query_context.parameters)
            
            # Save updated context to database
            await self._save_context_to_db(user_id, session_id, session_context)
            
            logging.info(f"Added query context: intent={query_context.intent}, category={query_context.category}")
        except Exception as e:
            logging.error(f"Error adding query context: {str(e)}")
    
    # NOTE: get_fallback_parameters() was removed (Oct 2025)
    
    async def reset_context(self, user_id: str, session_id: str):
        """Reset context for a session"""
        try:
            await delete_context_from_db(user_id, session_id)
            logging.info(f"Reset context for session {user_id}:{session_id}")
        except Exception as e:
            logging.error(f"Error resetting context: {str(e)}")
    
    
    # ======================================CONVERSATION HISTORY MANAGEMENT======================================
    
    async def get_contextual_conversation_history(self, user_id: str, session_id: str, max_tokens: int = 4000) -> Dict[str, Any]:
        """Retrieve conversation history optimized for LLM context window"""
        try:
            # Parallel database operations for optimal performance
            # DynamoDB handles concurrent requests efficiently
            conversations_task = get_session_conversations(user_id, session_id, limit=10)
            context_task = get_context_from_db(user_id, session_id)
            
            conversations, context_data = await asyncio.gather(conversations_task, context_task, return_exceptions=True)
            
            # Handle exceptions
            if isinstance(conversations, Exception):
                logging.error(f"Error getting conversations: {conversations}")
                conversations = []
            if isinstance(context_data, Exception):
                logging.error(f"Error getting context: {context_data}")
                context_data = None
            
            # Build optimized context
            context = {
                'recent_conversations': [],
                'structured_context': context_data,
                'token_count': 0,
                'context_summary': ""
            }
            
            # Process conversations in reverse chronological order
            for conv in reversed(conversations[-5:]):  # Last 5 conversations
                query_text = conv.get('query', '')
                response_text = conv.get('response', '')
                
                # Truncate response to prevent token overflow
                truncated_response = response_text[:500] if response_text else ""
                
                conv_tokens = self._estimate_tokens(query_text + truncated_response)
                
                if context['token_count'] + conv_tokens < max_tokens:
                    context['recent_conversations'].append({
                        'query': query_text,
                        'response': truncated_response,
                        'timestamp': conv.get('timestamp', ''),
                        'tokens': conv_tokens
                    })
                    context['token_count'] += conv_tokens
                else:
                    break
            
            logging.info(f"Retrieved {len(context['recent_conversations'])} conversations with {context['token_count']} tokens")
            return context
            
        except Exception as e:
            logging.error(f"Error getting contextual history: {str(e)}")
            return {'recent_conversations': [], 'structured_context': None, 'token_count': 0}
    
    async def build_contextual_prompt(self, user_query: str, context_data: Dict[str, Any], max_tokens: int = 4000) -> str:
        """Build an optimized prompt that includes conversation history while staying within token limits"""
        try:
            prompt_parts = []
            current_tokens = self._estimate_tokens(user_query)
            
            # Add conversation history if available and within token limits
            if context_data.get('recent_conversations') and current_tokens < max_tokens * 0.8:
                prompt_parts.append("CONVERSATION HISTORY:")
                
                for i, conv in enumerate(context_data['recent_conversations'], 1):
                    conv_text = f"Q{i}: {conv['query']}\nA{i}: {conv['response']}\n"
                    conv_tokens = self._estimate_tokens(conv_text)
                    
                    if current_tokens + conv_tokens < max_tokens * 0.8:
                        prompt_parts.append(conv_text)
                        current_tokens += conv_tokens
                    else:
                        break
                
                prompt_parts.append("\nIMPORTANT: Use conversation history for context understanding, but for real-time data (train times, schedules), ALWAYS use current API responses.")
                current_tokens += self._estimate_tokens(prompt_parts[-1])
            
            # CRITICAL: Do NOT include cached parameters in the contextual prompt for final response generation
            # Cached parameters should only be used for parameter enhancement during API call preparation
            # The final response must rely strictly on the current API response data
            # Structured context is excluded to prevent cached parameters from influencing the final response
            
            # Add current query
            prompt_parts.append(f"CURRENT QUERY: {user_query}")
            
            final_prompt = "\n".join(prompt_parts)
            final_tokens = self._estimate_tokens(final_prompt)
            
            # Final safety check
            if final_tokens > max_tokens:
                final_prompt = self._truncate_text_to_tokens(final_prompt, max_tokens)
                logging.warning(f"Truncated prompt from {final_tokens} to {self._estimate_tokens(final_prompt)} tokens")
            
            logging.info(f"Built contextual prompt with {self._estimate_tokens(final_prompt)} tokens")
            return final_prompt
            
        except Exception as e:
            logging.error(f"Error building contextual prompt: {str(e)}")
            return user_query
    
    # ======================================MAIN PROCESSING FUNCTIONS======================================
    
    async def update_context_after_processing(self, user_id: str, session_id: str, 
                                            original_query: str, processed_query: str,
                                            intent_data: Dict[str, Any], api_response: Dict[str, Any], query_type: str = None):
        """Update context after processing a query with the final combined response"""
        try:
            # Extract final response
            final_response = ""
            if api_response and "final_response" in api_response:
                final_response = api_response["final_response"]
            elif api_response:
                final_response = str(api_response)[:200]
            
            # CRITICAL FIX: Check if query is API-related FIRST, before checking query_type
            # A query classified as 'kb' can still be API-related (e.g., "what's the fare")
            is_api_related = intent_data.get("is_api_related", False)
            api_categories = ["ADVISORY", "REAL_TIME", "ROUTE", "SCHEDULE", "STATION"]
            category = intent_data.get("category", "")
            
            # Determine if we should store context
            should_store_context = False
            context_break_intents = ['greeting', 'stop_command', 'off_topic', 'repeat_request']  # Removed 'kb' from here
            
            if query_type == "api":
                # Explicitly classified as API
                should_store_context = True
                logging.info(f"Storing context for API intent: category={category}")
            elif is_api_related and category in api_categories:
                # API-related query (even if classified as 'kb')
                should_store_context = True
                logging.info(f"Storing context for API-related query: category={category}, query_type={query_type}")
            elif query_type in context_break_intents:
                # True context break intent (greeting, stop, etc.)
                should_store_context = False
                logging.info(f"Context break intent detected: {query_type} - resetting context")
                await self.reset_context(user_id, session_id)
            elif query_type == "kb":
                # KB queries should reset context to prevent stale API context from being used
                should_store_context = False
                logging.info(f"KB query detected: {query_type} - resetting context to prevent stale API context")
                await self.reset_context(user_id, session_id)
            else:
                # Other non-API queries - don't reset, but don't add new context
                should_store_context = False
                logging.info(f"Not storing context for non-API intent: {query_type}")
            
            if should_store_context:
                # Create query context
                query_context = QueryContext(
                    intent=query_type,
                    category=intent_data.get("category", "unknown"),
                    api_endpoint=intent_data.get("api_endpoint"),
                    parameters=intent_data.get("parameters", {}),
                    query_text=processed_query,
                    response_preview=final_response[:200] if final_response else ""
                )
                
                # Add to session context
                await self.add_query_context(user_id, session_id, query_context)
                logging.info(f"Updated context for user {user_id}: intent={query_context.intent}")
            else:
                logging.info(f"No context stored for intent: {query_type}")
                
                # Don't save session state to DB here to avoid overwriting the response
                # The response was already saved in main.py before calling this method
            
        except Exception as e:
            logging.error(f"Error updating context after processing: {str(e)}")
    
    async def get_contextual_prompt_wrapper(self, user_id: str, session_id: str, current_query: str) -> str:
        """Generate contextual prompt wrapper for LLM from database context"""
        try:
            session_context = await self.get_session_context(user_id, session_id)
            
            if not session_context.recent_contexts:
                return current_query
                
            # Get last relevant context
            last_context = session_context.last_query_context
            if not last_context:
                return current_query
                
            # Build contextual prompt
            prompt_parts = []
            
            if last_context.query_text:
                prompt_parts.append(f"Previous question: {last_context.query_text}")
            
            # CRITICAL FIX: Include previous category and endpoint to help detect category changes
            # This enables the LLM to recognize when the current query is about a different category
            # and should be treated as an independent query
            if hasattr(last_context, 'category') and last_context.category:
                prompt_parts.append(f"Previous category: {last_context.category}")
            
            if hasattr(last_context, 'api_endpoint') and last_context.api_endpoint:
                prompt_parts.append(f"Previous endpoint: {last_context.api_endpoint}")
                
            # Include the actual parameters from the previous API call to prevent hallucination
            if hasattr(last_context, 'parameters') and last_context.parameters:
                params = last_context.parameters
                
                if 'orig' in params and 'dest' in params:
                    prompt_parts.append(f"Previous trip: {params['orig']} to {params['dest']}")
                elif 'station' in params:
                    prompt_parts.append(f"Previous station: {params['station']}")
            
            prompt_parts.append(f"Current question: {current_query}")
            
            return "\n".join(prompt_parts)
        except Exception as e:
            logging.error(f"Error getting contextual prompt wrapper: {str(e)}")
            return current_query
    
    # ======================================UTILITY FUNCTIONS======================================
    
    def _estimate_tokens(self, text: str) -> int:
        """Estimate the number of tokens in a text string"""
        try:
            if text is None or not isinstance(text, str):
                return 0
            # Conservative estimation for Claude models (1 token ≈ 3 characters)
            return max(1, len(text) // 3)
        except Exception as e:
            logging.error(f"Error estimating tokens: {str(e)}")
            return max(1, len(str(text)) // 4)
    
    def _truncate_text_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit within a token limit"""
        try:
            if not text or not isinstance(text, str):
                return text
            
            current_tokens = self._estimate_tokens(text)
            if current_tokens <= max_tokens:
                return text
            
            ratio = max_tokens / current_tokens
            target_length = int(len(text) * ratio * 0.9)  # 0.9 for safety margin
            
            truncated = text[:target_length]
            if len(truncated) < len(text):
                truncated = truncated.rstrip() + "..."
            
            return truncated
        except Exception as e:
            logging.error(f"Error truncating text: {str(e)}")
            return text
    
    
    
    
    
    # ======================================DATABASE OPERATIONS======================================
    
    async def _save_context_to_db(self, user_id: str, session_id: str, session_context: SessionContext):
        """Save session context to database with structured data"""
        try:
            # Prepare context data for database storage
            context_data = {
                # Current query information (most recent)
                'current_query': {
                    'intent': session_context.last_intent or 'unknown',
                    'category': session_context.last_category or 'unknown', 
                    'api_endpoint': session_context.last_api_endpoint,
                    'parameters': session_context.cached_parameters
                },
                
                # Session metadata
                'conversation_count': session_context.conversation_count,
                'last_activity': session_context.last_activity.isoformat(),
                'last_response': session_context.last_response,
                'last_response_timestamp': session_context.last_response_timestamp.isoformat() if session_context.last_response_timestamp else None,
                
                # Historical context (last 5 queries)
                'query_history': []
            }
            
            # Log the response storage for debugging
            if session_context.last_response:
                logging.info(f"Storing response in context for {user_id}:{session_id} - Response length: {len(session_context.last_response)}")
                logging.info(f"Response preview: {session_context.last_response[:100]}...")
            else:
                logging.warning(f"No response to store in context for {user_id}:{session_id}")
            
            # Convert recent contexts to structured format for history
            for ctx in session_context.recent_contexts:
                if ctx:
                    query_info = {
                        'intent': ctx.intent,
                        'category': ctx.category,
                        'api_endpoint': ctx.api_endpoint,
                        'parameters': ctx.parameters,
                        'timestamp': ctx.timestamp.isoformat(),
                        'query_text': ctx.query_text,
                        'response_preview': ctx.response_preview
                    }
                    context_data['query_history'].append(query_info)
            
            # Log the structured data for debugging
            logging.info(f"Storing structured context data for {user_id}:{session_id}")
            logging.info(f"Current Query - Intent: {context_data['current_query']['intent']}, "
                        f"Category: {context_data['current_query']['category']}, "
                        f"API Endpoint: {context_data['current_query']['api_endpoint']}, "
                        f"Parameters: {context_data['current_query']['parameters']}")
            
            # Save to database
            await save_context_to_db(user_id, session_id, context_data)
            
        except Exception as e:
            logging.error(f"Error saving context to database: {str(e)}")
    
    async def cleanup_expired_contexts(self, expiry_hours: int = 24):
        """Clean up expired session contexts"""
        try:
            await cleanup_expired_contexts_from_db()
        except Exception as e:
            logging.error(f"Error in cleanup_expired_contexts: {str(e)}")
    
    async def cleanup_session_context(self, user_id: str, session_id: str) -> bool:
        """
        Clean up session context to prevent memory leaks.
        This is more aggressive than reset and is used during session cleanup.
        """
        try:
            # Delete the context from database
            await delete_context_from_db(user_id, session_id)
            
            # Force cleanup of any expired contexts while we're at it
            await cleanup_expired_contexts_from_db()
            
            logging.info(f"Session context cleaned up for {user_id}:{session_id}")
            return True
        except Exception as e:
            logging.error(f"Error cleaning up session context: {str(e)}")
            return False
    
# ======================================CONTEXT UTILITY FUNCTIONS======================================
# These functions are used by main.py and other modules

async def extract_parameters_with_context(query_text: str, session_context) -> Dict[str, Any]:
    """
    Extract parameters from query using context information.
    This is a simplified version that doesn't use AI for fixed BART stations.
    """
    if not session_context:
        return {}
    
    # Import here to avoid circular imports
    from apis import extract_date_type_from_query
    from main import extract_station_names

    
    # Get basic parameters from the query using existing logic
    params = {}
    
    # Extract station names from query
    station_names = extract_station_names(query_text)
    if station_names:
        if len(station_names) == 1:
            # Use orig parameter for single station (matches BART API schema)
            params["orig"] = station_names[0]
        # For multiple stations, let the AI determine orig/dest based on query context
        # Do not force station order here
    
    # Extract time-related parameters
    query_lower = query_text.lower()
    if "now" in query_lower:
        params["time"] = "now"
    
    # Extract date-related parameters
    date_type = extract_date_type_from_query(query_text)
    if date_type:
        params["date"] = date_type
    
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
    
    # Extract platform information
    platform_match = re.search(r'platform\s+(\d+)', query_lower)
    if platform_match:
        params["plat"] = platform_match.group(1)
    
    # Clean up None values
    return {k: v for k, v in params.items() if v is not None}

def is_similar_query(query1: str, query2: str) -> bool:
    """Check if two queries are similar enough to be considered duplicates"""
    try:
        q1_normalized = query1.lower().strip()
        q2_normalized = query2.lower().strip()
        
        if q1_normalized == q2_normalized:
            return True
        
        q1_clean = clean_query_for_comparison(q1_normalized)
        q2_clean = clean_query_for_comparison(q2_normalized)
        
        if q1_clean == q2_clean:
            return True
        
        similarity_score = calculate_query_similarity(q1_clean, q2_clean)
        return similarity_score > 0.8
    except Exception as e:
        logging.error(f"Error in query similarity check: {str(e)}")
        return False

def clean_query_for_comparison(query: str) -> str:
    """Clean query for comparison by removing variations that don't change meaning"""
    cleaned = re.sub(r'[^\w\s]', ' ', query)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def calculate_query_similarity(query1: str, query2: str) -> float:
    """Calculate similarity between two queries using word overlap"""
    try:
        words1 = set(query1.split())
        words2 = set(query2.split())
        
        if not words1 or not words2:
            return 0.0
        
        # Calculate Jaccard similarity
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        
        if union == 0:
            return 0.0
        
        jaccard_similarity = intersection / union
        
        # Boost similarity for queries with same key words
        key_words1 = {w for w in words1 if len(w) > 3}
        key_words2 = {w for w in words2 if len(w) > 3}
        
        if key_words1 and key_words2:
            key_overlap = len(key_words1.intersection(key_words2)) / len(key_words1.union(key_words2))
            return max(jaccard_similarity, key_overlap)
        
        return jaccard_similarity
    except Exception as e:
        logging.error(f"Error calculating query similarity: {str(e)}")
        return 0.0


# ======================================GLOBAL INSTANCE======================================
# Create global instance for production use
context_manager = ProductionContextManager()
