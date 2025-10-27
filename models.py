from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple


@dataclass
class SessionInfo:
    """Session information with fixed time expiration"""
    user_id: str
    session_id: str
    created_at: datetime
    expires_at: datetime
    language: str = 'en'
    last_active: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True

@dataclass
class QueryContext:
    """Stores context information for a query including intent and parameters"""
    intent: str
    category: str
    api_endpoint: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    query_text: str = ""
    response_preview: str = ""


@dataclass 
class SessionContext:
    """Enhanced session context for continuity"""
    user_id: str
    session_id: str
    last_query_context: Optional[QueryContext] = None
    recent_contexts: List[QueryContext] = field(default_factory=list)
    cached_parameters: Dict[str, Any] = field(default_factory=dict)
    last_intent: Optional[str] = None
    last_category: Optional[str] = None
    last_api_endpoint: Optional[str] = None
    conversation_count: int = 0
    last_activity: datetime = field(default_factory=datetime.utcnow)
    last_response: Optional[str] = None  # Store the last response for quick repeat
    last_response_timestamp: Optional[datetime] = None  # Track when response was generated
    
    def add_context(self, context: QueryContext):
        """Add a new query context and maintain recent history"""
        self.last_query_context = context
        self.recent_contexts.append(context)
        self.last_intent = context.intent
        self.last_category = context.category
        self.last_api_endpoint = context.api_endpoint
        self.conversation_count += 1
        self.last_activity = datetime.utcnow()
        
        # Keep only last 5 contexts to prevent memory bloat
        if len(self.recent_contexts) > 5:
            self.recent_contexts = self.recent_contexts[-5:]
    
    def store_response(self, response: str):
        """Store the last response for quick repeat functionality"""
        self.last_response = response
        self.last_response_timestamp = datetime.utcnow()
    
    def get_stored_response(self, max_age_minutes: int = 5) -> Optional[str]:
        """Get the stored response if it's recent enough"""
        if not self.last_response or not self.last_response_timestamp:
            return None
        
        # Check if response is recent enough (default 5 minutes)
        age = datetime.utcnow() - self.last_response_timestamp
        if age.total_seconds() > (max_age_minutes * 60):
            return None  # Response is too old
        
        return self.last_response
    
    def update_cached_parameters(self, new_params: Dict[str, Any]):
        """Update cached parameters with new values"""
        for key, value in new_params.items():
            if value is not None:
                self.cached_parameters[key] = value
                
    
    @staticmethod
    def get_endpoint_param_schema() -> Dict[str, List[str]]:
        """Define valid parameters for each API endpoint based on official BART API documentation"""
        return {
            # Advisory endpoints
            'bsa': ['orig'],
            'count': [],
            'ets': ['stn', 'eq'],
            
            # Real-time endpoints  
            'etd': ['orig', 'plat', 'dir'],
            
            # Route endpoints
            'routeinfo': ['route'],
            'routes': [],
            'routesched': ['route', 'date', 'show_legend'],
            
            # Schedule endpoints
            'arrive': ['orig', 'dest', 'time', 'date', 'b', 'a', 'l'],
            'depart': ['orig', 'dest', 'time', 'date', 'b', 'a', 'l'],
            'fare': ['orig', 'dest', 'date', 'sched'],
            'stnsched': ['orig', 'date'],
            'scheds': [],
            
            # Station endpoints
            'stninfo': ['orig'],
            'stns': [],
            'stnaccess': ['orig', 'show_legend']
        }
    
    @staticmethod
    def get_endpoint_date_formats() -> Dict[str, List[str]]:
        """
        Define supported date formats for each API endpoint
        
        CRITICAL: Only routesched endpoint accepts wd/sa/su format
        All other endpoints must use MM/DD/YYYY format
        """
        return {
            # ONLY routesched accepts wd/sa/su format
            'routesched': ['wd', 'sa', 'su', 'today', 'now', 'mm/dd/yyyy'],
            
            # All other endpoints ONLY accept MM/DD/YYYY format
            'arrive': ['mm/dd/yyyy', 'today', 'now'],
            'depart': ['mm/dd/yyyy', 'today', 'now'],
            'fare': ['mm/dd/yyyy', 'today', 'now'],
            'stnsched': ['mm/dd/yyyy', 'today', 'now'],
            'etd': ['mm/dd/yyyy', 'today', 'now'],
            'bsa': ['mm/dd/yyyy', 'today', 'now'],
            'ets': ['mm/dd/yyyy', 'today', 'now'],
            'stninfo': ['mm/dd/yyyy', 'today', 'now'],
            'stnaccess': ['mm/dd/yyyy', 'today', 'now'],
            
            # Endpoints that don't use date parameters
            'count': [],
            'routes': [],
            'routeinfo': [],
            'stns': [],
            'scheds': []
        }
    
    @staticmethod
    def get_endpoint_required_params() -> Dict[str, List[str]]:
        """Define REQUIRED parameters for each API endpoint"""
        return {
            # Advisory endpoints
            'bsa': [],  # orig is optional (defaults to 'all')
            'count': [],
            'ets': ['eq'],  # Equipment type is required
            
            # Real-time endpoints
            'etd': [],  # orig is optional (defaults to 'ALL')
            
            # Route endpoints
            'routeinfo': ['route'],
            'routes': [],
            'routesched': ['route'],
            
            # Schedule endpoints
            'arrive': ['orig', 'dest'],  # Both required
            'depart': ['orig', 'dest'],  # Both required
            'fare': ['orig', 'dest'],    # Both required
            'stnsched': ['orig'],
            'scheds': [],
            
            # Station endpoints
            'stninfo': ['orig'],
            'stns': [],
            'stnaccess': ['orig']
        }
    
    @staticmethod
    def get_endpoint_defaults(query_text: str = "") -> Dict[str, Dict[str, Any]]:
        """Define default values for optional parameters when not specified"""
        # Check query text for equipment type hint
        equipment_type = "elevator"
        if query_text and "escalator" in query_text.lower():
            equipment_type = "escalator"
        
        return {
            'etd': {'orig': 'ALL'},           # Show all stations if none specified
            'bsa': {'orig': 'all'},           # System-wide alerts if none specified  
            'ets': {'eq': equipment_type},    # Default equipment type
        }


class EndpointValidator:
    """
    Production-grade endpoint parameter validator.
    Centralized logic for validating, cleaning, and defaulting endpoint parameters.
    """
    
    @staticmethod
    def validate_and_prepare_params(
        endpoint: str, 
        params: Dict[str, Any], 
        query_text: str = ""
    ) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        Validate and prepare parameters for an API endpoint.
        
        Steps:
        1. Clean up null/invalid values
        2. Validate against endpoint schema (remove invalid params)
        3. Set mandatory defaults for optional params
        4. Validate required params are present
        
        Args:
            endpoint: API endpoint name
            params: Parameters from AI intent classification
            query_text: Original query text (for context-aware defaults)
        
        Returns:
            Tuple[bool, Dict[str, Any], Optional[str]]:
                - is_valid: True if parameters are valid
                - cleaned_params: Cleaned and validated parameters
                - error_message: Error message if validation failed, None otherwise
        """
        if not endpoint:
            return False, {}, "No endpoint specified"
        
        cleaned_params = params.copy()
        
        # Step 1: Clean up string null values
        params_to_remove = []
        for key in list(cleaned_params.keys()):
            if isinstance(cleaned_params[key], str) and cleaned_params[key].lower() in ["null", "none", "n/a", ""]:
                params_to_remove.append(key)
                print(f"[VALIDATOR] Removing null value for parameter '{key}': '{cleaned_params[key]}'")
        
        for key in params_to_remove:
            del cleaned_params[key]
        
        # Step 2: Validate against endpoint schema (remove invalid parameters)
        valid_params = SessionContext.get_endpoint_param_schema().get(endpoint, [])
        invalid_params = [key for key in cleaned_params.keys() if key not in valid_params]
        
        for key in invalid_params:
            print(f"[VALIDATOR] Removing invalid parameter '{key}' for endpoint '{endpoint}' (not in schema)")
            del cleaned_params[key]
        
        # Step 2.5: Validate date format for endpoint
        if 'date' in cleaned_params and cleaned_params['date']:
            date_formats = SessionContext.get_endpoint_date_formats().get(endpoint, [])
            date_value = cleaned_params['date'].lower()
            
            # Check if date format is supported for this endpoint
            is_valid_date_format = False
            if 'wd' in date_formats and date_value in ['wd', 'sa', 'su']:
                is_valid_date_format = True
            elif 'mm/dd/yyyy' in date_formats and (date_value in ['today', 'now'] or 
                                                   (len(date_value.split('/')) == 3 and 
                                                    all(part.isdigit() for part in date_value.split('/')))):
                is_valid_date_format = True
            
            if not is_valid_date_format:
                print(f"[VALIDATOR] Invalid date format '{cleaned_params['date']}' for endpoint '{endpoint}'")
                del cleaned_params['date']
        
        # Step 3: Set mandatory defaults for optional parameters
        endpoint_defaults = SessionContext.get_endpoint_defaults(query_text)
        defaults_for_endpoint = endpoint_defaults.get(endpoint, {})
        
        for param_name, default_value in defaults_for_endpoint.items():
            if param_name not in cleaned_params or cleaned_params[param_name] is None:
                cleaned_params[param_name] = default_value
                print(f"[VALIDATOR] Setting default for {endpoint}: {param_name}={default_value}")
        
        # Step 4: Validate required parameters are present
        required_params = SessionContext.get_endpoint_required_params().get(endpoint, [])
        missing_params = [param for param in required_params if param not in cleaned_params or cleaned_params[param] is None]
        
        if missing_params:
            error_msg = f"{endpoint} endpoint requires: {', '.join(missing_params)}"
            print(f"[VALIDATOR] Validation failed: {error_msg}")
            return False, cleaned_params, error_msg
        
        # Step 5: Validate same origin/destination for fare queries
        if endpoint == "fare" and "orig" in cleaned_params and "dest" in cleaned_params:
            if cleaned_params["orig"] == cleaned_params["dest"]:
                error_msg = "Fare queries cannot have same origin and destination"
                print(f"[VALIDATOR] Validation failed: {error_msg}")
                return False, cleaned_params, error_msg
        
        # All validations passed
        print(f"[VALIDATOR] Validation passed for {endpoint}: {cleaned_params}")
        return True, cleaned_params, None
