import asyncio
import json
import logging
import httpx
import re
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta
from functools import wraps
import time

# ======================================BART API CONFIGURATION======================================
import os
BART_API_KEY = os.getenv("BART_API_KEY")
HEADER_KEY = os.getenv("HEADER_KEY")
BART_BASE_URL = os.getenv("BART_BASE_URL")

# ======================================PRODUCTION-GRADE DECORATORS======================================
def api_timing_decorator(func):
    """Decorator to measure API call timing for production monitoring"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = time.time() 
        function_name = func.__name__
        
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            
            # Log successful API calls with timing
            if isinstance(result, dict) and "error" not in result:
                logging.info(f"✅ {function_name} completed successfully in {duration:.3f}s")
            else:
                logging.warning(f"⚠️ {function_name} completed with error in {duration:.3f}s")
            
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            logging.error(f"❌ {function_name} failed after {duration:.3f}s: {str(e)}")
            raise
            
    return wrapper

def validate_api_params(required_params: List[str] = None, optional_params: List[str] = None):
    """Decorator to validate API parameters before making calls"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if required_params:
                for param in required_params:
                    if param not in kwargs or kwargs[param] is None:
                        error_msg = f"Missing required parameter: {param}"
                        logging.error(f"❌ {func.__name__}: {error_msg}")
                        return {"error": error_msg}
            
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# ======================================STATION DATA======================================
# Import station data from constants
try:
    from constants import station_data, station_groups
    STATION_DATA = station_data
    STATION_GROUPS = station_groups
    logging.info(f"✅ Loaded {len(STATION_DATA.get('stations', []))} stations from constants")
except ImportError as e:
    logging.error(f"❌ Failed to import station data: {e}")
    # Fallback if constants not available
    STATION_DATA = {"stations": []}
    STATION_GROUPS = {}
except Exception as e:
    logging.error(f"❌ Error loading station data: {e}")
    STATION_DATA = {"stations": []}
    STATION_GROUPS = {}

# ======================================CORE BART API FUNCTIONS======================================
@api_timing_decorator
async def call_bart_api(endpoint: str, cmd: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Production-grade BART API client with comprehensive error handling
    
    Args:
        endpoint: BART API endpoint (e.g., 'etd', 'sched', 'stn', 'ets')
        cmd: Command for the endpoint (e.g., 'etd', 'depart', 'stninfo', 'status')
        params: Optional parameters for the API call
        
    Returns:
        Dict containing API response or error information
    """
    try:
        if params is None:
            params = {}
            
        # Validate station parameters
        station_params = []
        for key, value in params.items():
            if key in ['stn', 'orig', 'dest'] and value:
                station_params.append(str(value))
                
        if station_params:
            validation_result = validate_stations(station_params)
            if not validation_result["valid"]:
                invalid_stations = validation_result["invalid_stations"]
                invalid_station_str = ", ".join(invalid_stations)
                raise ValueError(f"Found invalid station(s): {invalid_station_str}")
        
        # Process station parameters
        new_params = {}
        for key, value in params.items():
            if value is None:
                continue
                
            if key in ['orig', 'dest', 'stn']:
                if str(value).upper() == "ALL":
                    new_params[key] = "ALL"
                else:
                    station_code = get_station_code(str(value))
                    if isinstance(station_code, tuple):
                        raise ValueError(f"Ambiguous station: {value}. Please specify which station you mean.")
                    if station_code:
                        new_params[key] = station_code
                    else:
                        raise ValueError(f"Invalid station: {value}")
            else:
                new_params[key] = value
        
        # Build URL
        url = f"{BART_BASE_URL}/{endpoint}.aspx"
        query_string = f"cmd={cmd}&key={BART_API_KEY}"
        
        if 'json' not in new_params:
            query_string += "&json=y"
            
        for key, value in new_params.items():
            query_string += f"&{key}={value}"
        
        full_url = f"{url}?{query_string}"
        logging.info(f"Making BART API call: {full_url}")
        
        # CRITICAL FIX: ETS endpoint requires API key as header
        # Build headers with API key for ETS endpoint
        headers = {}
        if endpoint == 'ets':
            headers['key'] = HEADER_KEY
            logging.info(f"Added API key header for ETS endpoint")
        
        # Make async HTTP request with headers
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(full_url, headers=headers)
        
        response.raise_for_status()
        
        if not response.content:
            raise ValueError("Empty response received from BART API")
            
        try:
            result = response.json()
        except ValueError as json_err:
            if response.headers.get('content-type', '').startswith('application/xml'):
                return {"error": "Received XML response. Please ensure JSON format is requested."}
            else:
                raise ValueError(f"Invalid JSON response: {str(json_err)}")
        
        logging.info(f"BART API response received successfully")
        
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response format: {type(result)}")
            
        return result
        
    except httpx.RequestError as re:
        error_msg = f"Network error calling BART API: {str(re)}"
        logging.error(error_msg)
        return {"error": error_msg}
    except httpx.HTTPStatusError as hse:
        error_msg = f"HTTP error calling BART API: {hse.response.status_code} - {str(hse)}"
        logging.error(error_msg)
        return {"error": error_msg}
    except ValueError as ve:
        error_msg = str(ve)
        logging.error(f"Validation error: {error_msg}")
        return {"error": error_msg}
    except Exception as e:
        error_msg = f"Error calling BART API: {str(e)}"
        logging.error(error_msg)
        return {"error": error_msg}

def get_station_code(station_name: str) -> Optional[str]:
    """
    Production-grade station code resolver with comprehensive matching logic
    
    Args:
        station_name: Station name or abbreviation to resolve
        
    Returns:
        Station abbreviation code or None if not found
    """
    if not station_name or not STATION_DATA.get("stations"):
        return None
        
    station_name_lower = station_name.lower().strip()
    
    # Remove common suffixes
    for suffix in [" station", " bart station", " bart"]:
        if station_name_lower.endswith(suffix):
            station_name_lower = station_name_lower[:-len(suffix)].strip()
            break
    
    # Handle SFO aliases with comprehensive matching
    sfo_aliases = ["sfo", "sfo airport", "san francisco airport", "sf airport", 
                   "san francisco international", "san francisco international airport"]
    for alias in sfo_aliases:
        if station_name_lower == alias or alias in station_name_lower:
            for station in STATION_DATA['stations']:
                if station['abbr'].lower() == "sfia":
                    logging.info(f"Found SFO airport match: {station['name']} -> {station['abbr']}")
                    return station['abbr']
    
    # 1. Check exact abbreviation match
    for station in STATION_DATA['stations']:
        if station['abbr'].lower() == station_name_lower:
            logging.info(f"Found exact abbreviation match: {station['abbr']}")
            return station['abbr']
    
    # 2. Check exact name match
    for station in STATION_DATA['stations']:
        if station['name'].lower() == station_name_lower:
            logging.info(f"Found exact name match: {station['name']} -> {station['abbr']}")
            return station['abbr']
    
    # 3. Check compound station names (e.g., "Dublin Pleasanton")
    for station in STATION_DATA['stations']:
        if '/' in station['name']:
            parts = [part.strip().lower() for part in station['name'].split('/')]
            if station_name_lower in parts:
                logging.info(f"Found match with part of station name: {station['name']} -> {station['abbr']}")
                return station['abbr']
    
    # 4. Check without spaces/punctuation
    station_name_no_spaces = station_name_lower.replace(' ', '').replace('-', '').replace('.', '')
    for station in STATION_DATA['stations']:
        station_no_spaces = station['name'].lower().replace(' ', '').replace('-', '').replace('.', '')
        if station_no_spaces == station_name_no_spaces:
            logging.info(f"Found match after removing spaces: {station['name']} -> {station['abbr']}")
            return station['abbr']
    
    # 5. Check partial matches (last resort)
    for station in STATION_DATA['stations']:
        if station_name_lower in station['name'].lower():
            logging.info(f"Found partial name match: {station['name']} -> {station['abbr']}")
            return station['abbr']
    
    logging.warning(f"No station code found for: {station_name}")
    return None

def map_user_station_to_actual_station(user_station: str) -> str:
    """
    Maps user-friendly station names (without slashes) to actual station names (with slashes).
    This handles cases where users say "Dublin Pleasanton" instead of "Dublin/Pleasanton".
    
    Args:
        user_station: The station name as provided by the user
        
    Returns:
        The actual station name from the station data, or the original if no mapping found
    """
    if not user_station:
        return user_station
    
    user_station_lower = user_station.lower().strip()
    
    # First, try exact matches
    for station in STATION_DATA["stations"]:
        actual_name = station["name"]
        actual_name_lower = actual_name.lower()
        
        # Exact match with actual name
        if user_station_lower == actual_name_lower:
            return actual_name
        
        # Exact match with abbreviation
        if user_station_lower == station["abbr"].lower():
            return actual_name
    
    # Create comprehensive mappings for compound station names
    station_mapping = {}
    
    for station in STATION_DATA["stations"]:
        actual_name = station["name"]
        actual_name_lower = actual_name.lower()
        
        # Map the actual name to itself
        station_mapping[actual_name_lower] = actual_name
        
        # Map abbreviation to actual name
        station_mapping[station["abbr"].lower()] = actual_name
        
        # For stations with slashes, create mappings for the parts
        if '/' in actual_name:
            parts = [part.strip().lower() for part in actual_name.split('/')]
            
            # Map individual parts to the full name
            for part in parts:
                station_mapping[part] = actual_name
            
            # Map concatenated parts (without slash) to the full name
            concatenated = ' '.join(parts)
            station_mapping[concatenated] = actual_name
            
            # Map with different separators
            station_mapping[' '.join(parts)] = actual_name
            station_mapping[''.join(parts)] = actual_name
    
    # Check if we have a direct mapping
    if user_station_lower in station_mapping:
        return station_mapping[user_station_lower]
    
    # Use fuzzy matching to find the best match
    best_match = None
    best_score = 0
    min_score_threshold = 0.6  # Minimum similarity threshold
    
    for mapped_name, actual_name in station_mapping.items():
        # Calculate similarity using different methods
        from difflib import SequenceMatcher
        
        # Method 1: Sequence matcher
        seq_score = SequenceMatcher(None, user_station_lower, mapped_name).ratio()
        
        # Method 2: Check if user input contains key words from the station name
        user_words = set(user_station_lower.split())
        mapped_words = set(mapped_name.split())
        word_overlap = len(user_words.intersection(mapped_words)) / max(len(user_words), len(mapped_words)) if mapped_words else 0
        
        # Method 3: Check if the station name contains key words from user input
        reverse_overlap = len(user_words.intersection(mapped_words)) / len(user_words) if user_words else 0
        
        # Combine scores (weighted average)
        combined_score = (seq_score * 0.4) + (word_overlap * 0.4) + (reverse_overlap * 0.2)
        
        if combined_score > best_score and combined_score >= min_score_threshold:
            best_score = combined_score
            best_match = actual_name
    
    if best_match:
        print(f"Fuzzy matched station: '{user_station}' → '{best_match}' (score: {best_score:.2f})")
        return best_match
    
    # If no good match found, return the original
    return user_station

def validate_stations(station_names: List[str], skip_fuzzy_matching: bool = False) -> Dict[str, Any]:
    """
    Validates if extracted station names are valid BART stations.
    Returns information about validity and suggestions for invalid stations.
    
    Args:
        station_names: List of station names to validate
    
    Returns:
        Dictionary with validation results containing:
        - valid: Boolean indicating if all stations are valid
        - invalid_stations: List of invalid station names
        - suggestions: Dictionary mapping each invalid station to suggested alternatives
    """
    result = {
        "valid": True,
        "invalid_stations": [],
        "suggestions": {},
        "params": {}
    }
    
    params = {}
    
    org_terms = ["bart","BART", "bay area rapid transit", "bay area rapid", "rapid transit"]
    
    filtered_names = []
    for name in station_names:
        if name and name.lower() not in org_terms:
            filtered_names.append(name)
    station_names = filtered_names
    
    valid_stations = {station["name"].lower(): station for station in STATION_DATA["stations"]}
    valid_abbrs = {station["abbr"].lower(): station for station in STATION_DATA["stations"]}
    
    sfo_aliases = [
        "sfo", "sfia", "sfo airport", "sf airport", 
        "san francisco airport", "san francisco international", "san francisco international airport",
        "sanfransisco airport", "sanfranssisco airport", "sanfransciso airport",  # Common typos
        "sanfransisco international airport", "sanfranssisco international airport", "sanfransciso international airport",  # Common typos with international
        "sf international airport", "sf international", "san francisco intl", "san francisco intl airport"
    ]
    
    sfia_station = None
    for station in STATION_DATA["stations"]:
        if station["abbr"].lower() == "sfia":
            sfia_station = station
            break
    
    filtered_station_names = []
    
    unique_stations = []
    station_mapping = {}
    full_station_names = {}
    
    for station in STATION_DATA["stations"]:
        if '/' in station["name"]:
            parts = [part.strip().lower() for part in station["name"].split('/')]
            for part in parts:
                full_station_names[part] = station["name"]
    
    for station_name in station_names:
        if not station_name:
            continue
            
        clean_name = station_name.rstrip('.')
        clean_name = re.sub(r'[,;:]', ' ', clean_name)  
        clean_name = re.sub(r'\s+', ' ', clean_name).strip()
        
        # CRITICAL FIX: Normalize spaces around slashes in station names
        # Handle cases like "Berryessa / North San Jose" -> "Berryessa/North San Jose"
        clean_name = re.sub(r'\s*/\s*', '/', clean_name)
        
        # Also handle other slash variations
        clean_name = re.sub(r'\s*\\\s*', '/', clean_name)  # Handle backslashes
        clean_name = re.sub(r'\s*-\s*', '/', clean_name)   # Handle dashes as separators
        
        # CRITICAL FIX: Normalize Street <-> St. before validation
        # This handles cases where AI extracts "19th Street Oakland" but station is "19th St. Oakland"
        clean_name = re.sub(r'\bStreet\b', 'St.', clean_name, flags=re.IGNORECASE)
        
        # Remove common station suffixes that users might add
        clean_name = re.sub(r'\s+station\s*$', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\s+stop\s*$', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\s+terminal\s*$', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\s+depot\s*$', '', clean_name, flags=re.IGNORECASE)
        
        clean_name_lower = clean_name.lower()
        
        # Try to map the station name to the actual station name (skip if in disambiguation flow)
        if not skip_fuzzy_matching:
            mapped_station = map_user_station_to_actual_station(clean_name)
            if mapped_station != clean_name:
                print(f"Mapped station in validation: '{clean_name}' → '{mapped_station}'")
                clean_name = mapped_station
                clean_name_lower = clean_name.lower()
        
        if sfia_station and (clean_name_lower in sfo_aliases or 
                            any(re.search(r'\b' + re.escape(alias) + r'\b', clean_name_lower) for alias in sfo_aliases)):
            print(f"Found SFO/Airport alias match in validation: {clean_name}")
            continue
        
        if clean_name_lower in valid_stations or clean_name_lower in valid_abbrs:
            continue
        
        clean_name_no_punct = re.sub(r'[^\w\s]', '', clean_name).strip()
        clean_name_no_punct_lower = clean_name_no_punct.lower()
        if clean_name_no_punct_lower in valid_stations or clean_name_no_punct_lower in valid_abbrs:
            continue
        
        # Try matching with different Street/St. combinations
        variations_to_check = [clean_name_lower, clean_name_no_punct_lower]
        
        # If has "st." or "st", also try with "street"
        if re.search(r'\bst\.?\b', clean_name_lower, re.IGNORECASE):
            street_version = re.sub(r'\bst\.?\b', 'street', clean_name_lower, flags=re.IGNORECASE)
            variations_to_check.append(street_version)
            street_no_punct = re.sub(r'[^\w\s]', '', street_version).strip()
            variations_to_check.append(street_no_punct)
        
        # Check all variations against valid stations
        found_match = False
        for variation in variations_to_check:
            if variation in valid_stations:
                print(f"Found valid station via Street/St. normalization: '{clean_name}' -> '{valid_stations[variation]['name']}'")
                found_match = True
                break
            if variation in valid_abbrs:
                print(f"Found valid station via Street/St. normalization: '{clean_name}' -> '{valid_abbrs[variation]['name']}'")
                found_match = True
                break
        
        if found_match:
            continue
        
        is_part_of_slash_station = False
        full_station_name = None
        
        if clean_name_lower in full_station_names:
            is_part_of_slash_station = True
            full_station_name = full_station_names[clean_name_lower]
            print(f"Found part of slash station: '{clean_name}' is part of '{full_station_name}'")
        else:
            for station in STATION_DATA["stations"]:
                if '/' in station["name"]:
                    parts = [part.strip().lower() for part in station["name"].split('/')]
                    if clean_name_lower in parts:
                        is_part_of_slash_station = True
                        full_station_name = station["name"]
                        print(f"Found part of slash station: '{clean_name}' is part of '{station['name']}'")
                        break
                    # Also check if the clean name matches the full station name (for exact matches)
                    if clean_name_lower == station["name"].lower():
                        is_part_of_slash_station = True
                        full_station_name = station["name"]
                        print(f"Found exact slash station match: '{clean_name}' matches '{station['name']}'")
                        break
        
        if is_part_of_slash_station:
            for key, value in list(params.items()):
                if key in ['orig', 'dest', 'station', 'stn'] and value and value.lower() == clean_name_lower:
                    params[key] = full_station_name
                    print(f"Replaced parameter {key}: '{value}' → '{full_station_name}'")
            continue
            
        is_substring = False
        for full_name in valid_stations.keys():
            if (clean_name_lower != full_name and clean_name_lower in full_name) or \
               (clean_name_no_punct_lower != full_name and clean_name_no_punct_lower in full_name):
                is_substring = True
                break
                
        if not is_substring:
            found_match = False
            for valid_name, station_info in valid_stations.items():
                valid_name_no_punct = re.sub(r'[^\w\s]', '', valid_name).strip()
                if clean_name_no_punct_lower == valid_name_no_punct.lower():
                    found_match = True
                    break
                    
            if found_match:
                continue
                
            filtered_station_names.append(station_name)
    
    for station_name in filtered_station_names:
        clean_name = station_name.rstrip('.')
        clean_name = re.sub(r'[,;:]', ' ', clean_name)  
        clean_name = re.sub(r'\s+', ' ', clean_name).strip()
        
        # CRITICAL FIX: Normalize spaces around slashes in station names
        # Handle cases like "Berryessa / North San Jose" -> "Berryessa/North San Jose"
        clean_name = re.sub(r'\s*/\s*', '/', clean_name)
        
        # Also handle other slash variations
        clean_name = re.sub(r'\s*\\\s*', '/', clean_name)  # Handle backslashes
        clean_name = re.sub(r'\s*-\s*', '/', clean_name)   # Handle dashes as separators
        
        # Remove common station suffixes that users might add
        clean_name = re.sub(r'\s+station\s*$', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\s+stop\s*$', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\s+terminal\s*$', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\s+depot\s*$', '', clean_name, flags=re.IGNORECASE)  
        
        result["valid"] = False
        result["invalid_stations"].append(station_name)
        
        first_letter = clean_name[0].lower()
        suggestions = []
        
        for valid_name in valid_stations.keys():
            if valid_name.lower().startswith(first_letter):
                suggestions.append(valid_stations[valid_name]["name"])
                
        if not suggestions:
            suggestions = [station["name"] for station in STATION_DATA["stations"][:5]]
            
        result["suggestions"][station_name] = suggestions
    
    result["params"] = params
            
    return result

def get_route_from_color(color_name: str) -> Optional[List[str]]:
    """Map BART line color names to route numbers
    
    Args:
        color_name (str): Color name (Yellow, Orange, Green, Red, Blue,Grey)
        
    Returns:
        Optional[List[str]]: List of route numbers corresponding to the color, or None if color not found
    """
    color_map = {
       "yellow": ["1", "2"],
       "orange": ["3", "4"],
       "green": ["5", "6"],
       "red": ["7", "8"],
       "blue": ["11", "12"],
       "grey": ["19", "20"]
    }
    
    if not color_name:
        return None
        
    normalized_color = color_name.lower().strip()
    return color_map.get(normalized_color)

def extract_route_color_from_query(query_text: str) -> Optional[str]:
    """Extract BART line color from user query
    
    Args:
        query_text (str): User query text
        
    Returns:
        Optional[str]: Extracted color name or None if no color found
    """
    import re
    
    query_lower = query_text.lower()
    
    combined_patterns = {
        r'\byellowline?\b': "yellow",
        r'\borangeline?\b': "orange",
        r'\bgreenline?\b': "green",
        r'\bredline?\b': "red",
        r'\bblueline?\b': "blue",
        r'\bgr[ae]yline?\b': "grey"
    }
    
    for pattern, color in combined_patterns.items():
        if re.search(pattern, query_lower):
            logging.info(f"Extracted color '{color}' from combined pattern in query: '{query_text}'")
            return color
    
    color_patterns = [
        r'\byellow\s*(line|train|route|bart)?s?\b',
        r'\borange\s*(line|train|route|bart)?s?\b', 
        r'\bgreen\s*(line|train|route|bart)?s?\b',
        r'\bred\s*(line|train|route|bart)?s?\b',
        r'\bblue\s*(line|train|route|bart)?s?\b',
        r'\bgr[ae]y\s*(line|train|route|bart)?s?\b'  
    ]
    
    for pattern in color_patterns:
        match = re.search(pattern, query_lower)
        if match:
            color = match.group(0).split()[0]
            if color in ["gray", "grey"]:
                color = "grey"
            logging.info(f"Extracted color '{color}' from query: '{query_text}'")
            return color
            
    return None


def extract_train_count_from_query(query_text: str) -> Optional[int]:
    """
    Extract the number of trains mentioned in a query.
    
    Args:
        query_text (str): The query text to analyze
        
    Returns:
        Optional[int]: The number of trains mentioned, or None if not found
    """
    query_lower = query_text.lower()
    
    number_words = {
        "one": 1, "two": 2, "three": 3, 
        "1": 1, "2": 2, "3": 3
    }
    
    for word, num in number_words.items():
        if f"{word} train" in query_lower:
            return num
    
    return None

def normalize_date_format(date_str: str, endpoint: str = None) -> Optional[str]:
    """
    Normalize various date formats based on API endpoint requirements
    
    CRITICAL: Only routesched endpoint accepts wd/sa/su format
    All other endpoints must use MM/DD/YYYY format
    
    Args:
        date_str: Date string to normalize
        endpoint: API endpoint name to determine format requirements
        
    Returns:
        Normalized date string appropriate for the endpoint
    """
    if not date_str:
        return None
        
    date_str = date_str.lower().strip()
    
    # Handle special date formats
    if date_str in ['today', 'now']:
        return date_str
    
    # CRITICAL FIX: Only routesched endpoint accepts wd/sa/su format
    if date_str in ['wd', 'sa', 'su']:
        if endpoint == 'routesched':
            return date_str
        else:
            # For all other endpoints, convert to MM/DD/YYYY format
            return convert_weekday_to_date(date_str)
    
    # Handle full weekday names - convert to appropriate format based on endpoint
    weekday_map = {
        'monday': 'wd', 'tuesday': 'wd', 'wednesday': 'wd', 
        'thursday': 'wd', 'friday': 'wd',
        'saturday': 'sa', 'sunday': 'su'
    }
    
    if date_str in weekday_map:
        weekday_abbr = weekday_map[date_str]
        if endpoint == 'routesched':
            return weekday_abbr
        else:
            # For all other endpoints, convert to MM/DD/YYYY format
            # Use specific weekday conversion for individual weekdays
            return convert_specific_weekday_to_date(date_str)
    
    # Handle MM/DD/YYYY format (already correct for most endpoints)
    try:
        for fmt in ['%m/%d/%Y', '%m-%d-%Y', '%Y-%m-%d', '%m/%d/%y']:
            try:
                parsed_date = datetime.strptime(date_str, fmt)
                return parsed_date.strftime('%m/%d/%Y')
            except ValueError:
                continue
        raise ValueError(f"Unsupported date format: {date_str}")
    except Exception as e:
        logging.error(f"Error normalizing date format: {e}")
        return None

def convert_weekday_to_date(weekday_abbr: str) -> str:
    """
    Convert weekday abbreviation (wd/sa/su) to next occurrence date in MM/DD/YYYY format
    
    Args:
        weekday_abbr: Weekday abbreviation ('wd', 'sa', 'su')
        
    Returns:
        Date string in MM/DD/YYYY format
    """
    try:
        today = datetime.now()
        current_weekday = today.weekday()  # 0=Monday, 6=Sunday
        
        # Map weekday abbreviations to Python weekday numbers
        weekday_targets = {
            'wd': [0, 1, 2, 3, 4],  # Monday-Friday
            'sa': [5],               # Saturday
            'su': [6]                # Sunday
        }
        
        target_days = weekday_targets.get(weekday_abbr, [])
        if not target_days:
            return today.strftime('%m/%d/%Y')
        
        # Find next occurrence
        days_ahead = None
        for target_day in target_days:
            if target_day >= current_weekday:
                days_ahead = target_day - current_weekday
                break
        
        # If no target day found in current week, get first occurrence next week
        if days_ahead is None:
            days_ahead = (7 - current_weekday) + min(target_days)
        
        target_date = today + timedelta(days=days_ahead)
        return target_date.strftime('%m/%d/%Y')
        
    except Exception as e:
        logging.error(f"Error converting weekday to date: {e}")
        return datetime.now().strftime('%m/%d/%Y')

def convert_specific_weekday_to_date(weekday_name: str) -> str:
    """
    Convert specific weekday name to next occurrence date in MM/DD/YYYY format
    
    Args:
        weekday_name: Full weekday name ('monday', 'tuesday', etc.)
        
    Returns:
        Date string in MM/DD/YYYY format
    """
    try:
        today = datetime.now()
        current_weekday = today.weekday()  # 0=Monday, 6=Sunday
        
        # Map weekday names to Python weekday numbers
        weekday_map = {
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4,
            'saturday': 5, 'sunday': 6
        }
        
        target_weekday = weekday_map.get(weekday_name.lower())
        if target_weekday is None:
            return today.strftime('%m/%d/%Y')
        
        # Calculate days ahead to next occurrence
        days_ahead = target_weekday - current_weekday
        if days_ahead <= 0:  # If it's the same day or past, get next week
            days_ahead += 7
        
        target_date = today + timedelta(days=days_ahead)
        return target_date.strftime('%m/%d/%Y')
        
    except Exception as e:
        logging.error(f"Error converting specific weekday to date: {e}")
        return datetime.now().strftime('%m/%d/%Y')

def normalize_time_format(time_str: str) -> Optional[str]:
    """Normalize time string to BART API format (h:mm+am/pm)
    
    Args:
        time_str: Time string in various formats
        
    Returns:
        Normalized time string or None if invalid
    """
    if not time_str:
        return None
        
    time_str = time_str.strip().lower()
    
    if time_str == 'now':
        return 'now'
    
    time_str = time_str.replace('.', '')
    
    if ' am' in time_str:
        time_str = time_str.replace(' am', 'am')
    elif ' pm' in time_str:
        time_str = time_str.replace(' pm', 'pm')
    
    try:
        time_obj = None
        
        if 'am' in time_str or 'pm' in time_str:
            formats = [
                '%I:%M%p',  
                '%I%M%p',   
                '%I%p'      
            ]
            
            for fmt in formats:
                try:
                    time_obj = datetime.strptime(time_str, fmt)
                    break
                except ValueError:
                    continue
                    
        else:
            formats = [
                '%H:%M',    
                '%H%M',     
                '%H'        
            ]
            
            for fmt in formats:
                try:
                    time_obj = datetime.strptime(time_str, fmt)
                    break
                except ValueError:
                    continue
        
        if time_obj is None:
            raise ValueError(f"Could not parse time: {time_str}")
            
        return time_obj.strftime('%-I:%M%p').lower()
    except Exception as e:
        logging.error(f"Error normalizing time format: {time_str} - {e}")
        
        try:
            if re.match(r'^\d{1,2}(am|pm)$', time_str):
                hour = re.match(r'^(\d{1,2})(am|pm)$', time_str)
                if hour:
                    hour_val = int(hour.group(1))
                    period = hour.group(2)
                    if 1 <= hour_val <= 12:
                        return f"{hour_val}:00{period}"
            
            if re.match(r'^\d{3,4}$', time_str):
                if len(time_str) == 3:  
                    hour = time_str[0]
                    minute = time_str[1:3]
                    hour_val = int(hour)
                    if 0 <= hour_val <= 9 and 0 <= int(minute) <= 59:
                        period = "am" if hour_val < 12 else "pm"
                        if hour_val == 0:
                            hour_val = 12
                        if hour_val > 12:
                            hour_val -= 12
                        return f"{hour_val}:{minute}{period}"
                elif len(time_str) == 4:  
                    hour = time_str[0:2]
                    minute = time_str[2:4]
                    hour_val = int(hour)
                    if 0 <= hour_val <= 23 and 0 <= int(minute) <= 59:
                        period = "am" if hour_val < 12 else "pm"
                        if hour_val == 0:
                            hour_val = 12
                        if hour_val > 12:
                            hour_val -= 12
                        return f"{hour_val}:{minute}{period}"
        except Exception:
            pass
            
        return None

def extract_date_type_from_query(query_text: str) -> Optional[str]:
    """
    Extract date type (weekday, saturday, sunday) from user query
    
    Args:
        query_text: User query text
        
    Returns:
        'wd' for weekday mentions
        'sa' for saturday mentions  
        'su' for sunday mentions
        None if no date type detected
    """
    query_lower = query_text.lower()
    
    weekday_patterns = [
        r'\b(weekday|week day|week days|weekdays)\b',
        r'\b(monday|tuesday|wednesday|thursday|friday)\b',
        r'\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri)\b'
    ]
    
    for pattern in weekday_patterns:
        if re.search(pattern, query_lower):
            logging.info(f"Detected weekday mention in query: '{query_text}'")
            return 'wd'
    
    saturday_patterns = [
        r'\b(saturday|saturdays)\b',
        r'\b(sat)\b'
    ]
    
    for pattern in saturday_patterns:
        if re.search(pattern, query_lower):
            logging.info(f"Detected Saturday mention in query: '{query_text}'")
            return 'sa'
    
    sunday_patterns = [
        r'\b(sunday|sundays)\b',
        r'\b(sun)\b'
    ]
    
    for pattern in sunday_patterns:
        if re.search(pattern, query_lower):
            logging.info(f"Detected Sunday mention in query: '{query_text}'")
            return 'su'
    
    return None

# ======================================BART API ENDPOINTS======================================
# ======================================ADVISORY ENDPOINTS======================================
async def get_service_advisories(
    station: str = 'all',
    json_output: Optional[bool] = True
):
    """Get service advisories
    
    Args:
        station (str): Station abbreviation or 'all' for all stations. Default is 'all'.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        params = {'orig': station}
        if json_output:
            params['json'] = 'y'
        return await call_bart_api('bsa', 'bsa', params)
    except Exception as e:
        logging.error(f"Error fetching service advisories: {e}")
        return {"error": str(e)}
    
async def get_train_count(
    json_output: Optional[bool] = True
):
    """Get number of trains currently active
    
    Args:
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        params = {}
        if json_output:
            params['json'] = 'y'
        return await call_bart_api('bsa', 'count', params)
    except Exception as e:
        logging.error(f"Error fetching train count: {e}")
        return {"error": str(e)}

async def get_equipment_status(
    eq: str = "elevator",
    stn: Optional[str] = None,
    json_output: Optional[bool] = True
):
    """Get elevator/escalator status information
    
    Args:
        eq (str): Equipment type - 'elevator' or 'escalator'. Default is 'elevator'.
        stn (str, optional): Station code to filter results. Default is None (all stations).
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        params = {
            'eq': eq.lower()
        }
        if stn:
            params['stn'] = stn
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('ets', 'status', params)
    except Exception as e:
        logging.error(f"Error fetching equipment status: {e}")
        return {"error": str(e)}

# ======================================REAL-TIME ENDPOINTS======================================
@api_timing_decorator
@validate_api_params(required_params=['station'])
async def get_real_time_departures(
    station: str = 'ALL',
    platform: Optional[str] = None,
    direction: Optional[str] = None,
    gb_color: Optional[bool] = False,
    json_output: Optional[bool] = True
):
    """Get real-time departure information
    
    Args:
        station (str): Station abbreviation or 'ALL' for all stations. Default is 'ALL'.
        platform (str, optional): Limit results to a specific platform (1-4).
        direction (str, optional): Limit results to 'n' (Northbound) or 's' (Southbound).
        gb_color (bool, optional): Sort response by line color if True. Default is False.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        params = {}
        
        if station.upper() != 'ALL':
            station_code = get_station_code(station)
            if not station_code:
                return {"error": f"Invalid station: {station}"}
            params['orig'] = station_code
            logging.info(f"Getting departures for station {station} using code {station_code}")
        else:
            params['orig'] = 'ALL'
        
        if platform:
            if not platform.isdigit() or not (1 <= int(platform) <= 4):
                return {"error": "Platform must be a number between 1 and 4"}
            params['plat'] = platform
            
        if direction:
            direction = direction.lower()
            if direction not in ['n', 's']:
                return {"error": "Direction must be 'n' for Northbound or 's' for Southbound"}
            params['dir'] = direction
            
        if gb_color:
            params['gbColor'] = '1'
            
        if json_output:
            params['json'] = 'y'
        
        return await call_bart_api('etd', 'etd', params)
    except Exception as e:
        logging.error(f"Error fetching real-time departures: {e}")
        return {"error": str(e)}

# ======================================STATION ENDPOINTS=====================================
async def get_station_info(
    station: str,
    json_output: Optional[bool] = True
):
    """Get detailed information for a specific station
    
    Args:
        station (str): Station abbreviation (4-character code).
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        station_code = get_station_code(station)
        if not station_code:
            return {"error": f"Invalid station: {station}"}
        
        params = {'orig': station_code}
        logging.info(f"Getting station info for {station} using code {station_code}")
        
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('stn', 'stninfo', params)
    except Exception as e:
        logging.error(f"Error fetching station info: {e}")
        return {"error": str(e)}

async def get_all_stations(
    json_output: Optional[bool] = True
):
    """Get list of all BART stations with details
    
    Args:
        json_output (bool, optional): Return JSON format if True. Default is True.
        
    Returns:
        List of all stations with their full names, abbreviations, latitude, longitude and addresses.
    """
    try:
        params = {}
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('stn', 'stns', params)
    except Exception as e:
        logging.error(f"Error fetching stations list: {e}")
        return {"error": str(e)}

async def get_station_access(
    station: str,
    show_legend: Optional[bool] = False,
    json_output: Optional[bool] = True
):
    """Get access and neighborhood information for a specific station
    
    Args:
        station (str): Station abbreviation (4-character code).
        show_legend (bool, optional): Include legend information. Default is False.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        station_code = get_station_code(station)
        if not station_code:
            return {"error": f"Invalid station: {station}"}
            
        params = {'orig': station_code}
        
        if show_legend:
            params['l'] = '1'
            
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('stn', 'stnaccess', params)
    except Exception as e:
        logging.error(f"Error fetching station access info: {e}")
        return {"error": str(e)}

# ======================================ROUTE ENDPOINTS======================================
async def get_route_info(
    route: str = '1',
    date: Optional[str] = None,
    json_output: Optional[bool] = True
):
    """Get detailed information for a specific route
    
    Args:
        route (str): Route number (1-12),19,20 'all' for all routes, or color name (Yellow, Orange, Green, Red, Blue, Grey).
        date (str, optional): Specific date in mm/dd/yyyy format, 'today', or 'now'.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        route_param = route.lower()
        
        import re
        for color in ["yellow", "orange", "green", "red", "blue", "grey", "gray"]:
            if re.search(f"{color}line?", route_param):
                route_param = color
                break
        
        if not (route_param == 'all' or (route_param.isdigit() and 1 <= int(route_param) <= 12)):
            route_numbers = get_route_from_color(route_param)
            
            if route_numbers:
                route_param = route_numbers[0]
                logging.info(f"Mapped color '{route}' to route number '{route_param}'")
            elif route_param != 'all':
                return {"error": "Route must be a number between 1 and 12, 'all', or a valid line color (Yellow, Orange, Green, Red, Blue)"}
        
        params = {'route': route_param}
            
        if date:
            if date.lower() in ['today', 'now']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', or 'now'"}
                
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('route', 'routeinfo', params)
    except Exception as e:
        logging.error(f"Error fetching route info: {e}")
        return {"error": str(e)}
    
async def get_all_routes(
    date: Optional[str] = None,
    json_output: Optional[bool] = True
):
    """Get information about all current routes/lines
    
    Args:
        date (str, optional): Specific date in mm/dd/yyyy format, 'today', or 'now'.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        params = {}
        
        if date:
            if date.lower() in ['today', 'now']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', or 'now'"}
                
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('route', 'routes', params)
    except Exception as e:
        logging.error(f"Error fetching routes list: {e}")
        return {"error": str(e)}
    
# ======================================SCHEDULE ENDPOINTS======================================
async def get_route_schedule(
    route: str,
    date: Optional[str] = None,
    show_legend: Optional[bool] = False,
    json_output: Optional[bool] = True
):
    """Get detailed schedule information for a specific route
    
    Args:
        route (str): Route number (1-12),19,20 or color name (Yellow, Orange, Green, Red, Blue, Grey).
        date (str, optional): Date in mm/dd/yyyy format, 'today', 'now', 'wd', 'sa', or 'su'.
        show_legend (bool, optional): Include legend information. Default is False.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        route_param = route.lower()
        
        import re
        for color in ["yellow", "orange", "green", "red", "blue", "grey", "gray"]:
            if re.search(f"{color}line?", route_param):
                route_param = color
                break
        
        if not (route_param.isdigit() and 1 <= int(route_param) <= 12):
            route_numbers = get_route_from_color(route_param)
            
            if route_numbers:
                route_param = route_numbers[0]
                logging.info(f"Mapped color '{route}' to route number '{route_param}'")
            else:
                return {"error": "Route must be a number between 1 and 12,19,20 or a valid line color (Yellow, Orange, Green, Red, Blue, Grey)"}
            
        params = {'route': route_param}
        
        if date:
            if date.lower() in ['today', 'now', 'wd', 'sa', 'su']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', 'now', 'wd', 'sa', or 'su'"}
        
        if show_legend:
            params['l'] = '1'
            
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('sched', 'routesched', params)
    except Exception as e:
        logging.error(f"Error fetching route schedule: {e}")
        return {"error": str(e)}
    
async def get_schedules_list(
    json_output: Optional[bool] = True
):
    """Get list of current BART schedules
    
    Args:
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        params = {}
        if json_output:
            params['json'] = 'y'
        return await call_bart_api('sched', 'scheds', params)
    except Exception as e:
        logging.error(f"Error fetching schedules list: {e}")
        return {"error": str(e)}

async def get_fare_info(
    orig: str,
    dest: str,
    date: Optional[str] = None,
    sched: Optional[str] = None,
    json_output: Optional[bool] = True
):
    """Get fare information between two stations
    
    Args:
        orig (str): Origin station abbreviation.
        dest (str): Destination station abbreviation.
        date (str, optional): Date in mm/dd/yyyy format, 'today', or 'now'.
        sched (str, optional): Specific schedule number.
        json_output (bool, optional): Return JSON format if True. Default is True.
        
    Note: date and sched parameters should not be used together.
    If both are provided, date will be ignored and sched will be used.
    """
    try:
        orig_code = get_station_code(orig)
        if not orig_code:
            return {"error": f"Invalid origin station: {orig}"}
            
        dest_code = get_station_code(dest)
        if not dest_code:
            return {"error": f"Invalid destination station: {dest}"}
        
        logging.info(f"Getting fare from {orig} ({orig_code}) to {dest} ({dest_code})")
        params = {
            'orig': orig_code,
            'dest': dest_code
        }
        
        if sched:
            if not sched.isdigit():
                return {"error": "Schedule must be a number"}
            params['sched'] = sched
        elif date:
            if date.lower() in ['today', 'now']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', or 'now'"}
                
        if json_output:
            params['json'] = 'y'
        
        return await call_bart_api('sched', 'fare', params)
    except Exception as e:
        logging.error(f"Error fetching fare info: {e}")
        return {"error": str(e)}

async def get_schedule_arrivals(
    orig: str,
    dest: str,
    time: Optional[str] = None,
    date: Optional[str] = None,
    before: Optional[int] = 0,
    after: Optional[int] = 3,
    show_legend: Optional[bool] = False,
    json_output: Optional[bool] = True
):
    """Get schedule arrivals
    
    Args:
        orig (str): Origin station abbreviation.
        dest (str): Destination station abbreviation.
        time (str, optional): Arrival time in h:mm+am/pm format or 'now'. Defaults to current time.
        date (str, optional): Date in mm/dd/yyyy format, 'today', or 'now'. Range: -10 days to +8 weeks.
        before (int, optional): Number of trips before specified time (0-3). Default is 0.
        after (int, optional): Number of trips after specified time (1-3). Default is 3.
        show_legend (bool, optional): Include legend information. Default is False.
        json_output (bool, optional): Return JSON format if True. Default is True.
        
    Note: Sum of before and after must be ≤ 6.
    """
    try:
        orig_code = get_station_code(orig)
        dest_code = get_station_code(dest)
        if not orig_code or not dest_code:
            return {"error": "Invalid station(s)"}
            
        if not (0 <= before <= 3):
            return {"error": "Before parameter must be between 0 and 3"}
        if not (1 <= after <= 3):
            return {"error": "After parameter must be between 1 and 3"}
        if (before + after) > 6:
            before = 0  
            after = 3  
            
        params = {
            'orig': orig_code,
            'dest': dest_code,
            'b': str(after),
            'a': str(before)
        }
        
        if time:
            if time.lower() == 'now':
                params['time'] = 'now'
            else:
                try:
                    datetime.strptime(time, '%I:%M%p')
                    params['time'] = time
                except ValueError:
                    return {"error": "Time must be in h:mm+am/pm format or 'now'"}
        
        if date:
            if date.lower() in ['today', 'now']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', or 'now'"}
                    
        if show_legend:
            params['l'] = '1'
            
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('sched', 'arrive', params)
    except Exception as e:
        logging.error(f"Error fetching schedule arrivals: {e}")
        return {"error": str(e)}

@api_timing_decorator
@validate_api_params(required_params=['orig', 'dest'])
async def get_schedule_departures(
    orig: str,
    dest: str,
    time: Optional[str] = None,
    date: Optional[str] = None,
    before: Optional[int] = 0,
    after: Optional[int] = 3,
    show_legend: Optional[bool] = False,
    json_output: Optional[bool] = True
):
    """Get schedule departures
    
    Args:
        orig (str): Origin station abbreviation.
        dest (str): Destination station abbreviation.
        time (str, optional): Departure time in h:mm+am/pm format or 'now'. Defaults to current time.
        date (str, optional): Date in mm/dd/yyyy format, 'today', or 'now'. Range: -10 days to +8 weeks.
        before (int, optional): Number of trips before specified time (0-3). Default is 0.
        after (int, optional): Number of trips after specified time (1-3). Default is 3.
        show_legend (bool, optional): Include legend information. Default is False.
        json_output (bool, optional): Return JSON format if True. Default is True.
        
    Note: Sum of before and after must be ≤ 6.
    """
    try:
        orig_code = get_station_code(orig)
        dest_code = get_station_code(dest)
        if not orig_code or not dest_code:
            return {"error": "Invalid station(s)"}
            
        if not (0 <= before <= 3):
            return {"error": "Before parameter must be between 0 and 3"}
        if not (1 <= after <= 3):
            return {"error": "After parameter must be between 1 and 3"}
        if (before + after) > 6:
            before = 0  
            after = 3  
            
        params = {
            'orig': orig_code,
            'dest': dest_code,
            'b': str(before),
            'a': str(after)
        }
        
        if time:
            if time.lower() == 'now':
                params['time'] = 'now'
            else:
                try:
                    datetime.strptime(time, '%I:%M%p')
                    params['time'] = time
                except ValueError:
                    return {"error": "Time must be in h:mm+am/pm format or 'now'"}
        
        if date:
            if date.lower() in ['today', 'now']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', or 'now'"}
                    
        if show_legend:
            params['l'] = '1'
            
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('sched', 'depart', params)
    except Exception as e:
        logging.error(f"Error fetching schedule departures: {e}")
        return {"error": str(e)}

async def get_station_schedule(
    station: str,
    date: Optional[str] = None,
    json_output: Optional[bool] = True
):
    """Get detailed schedule information for a specific station
    
    Args:
        station (str): Station abbreviation.
        date (str, optional): Date in mm/dd/yyyy format, 'today', or 'now'.
        json_output (bool, optional): Return JSON format if True. Default is True.
    """
    try:
        station_code = get_station_code(station)
        if not station_code:
            return {"error": f"Invalid station: {station}"}
            
        params = {'orig': station_code}
        
        if date:
            if date.lower() in ['today', 'now']:
                params['date'] = date.lower()
            else:
                try:
                    datetime.strptime(date, '%m/%d/%Y')
                    params['date'] = date
                except ValueError:
                    return {"error": "Date must be in mm/dd/yyyy format, 'today', or 'now'"}
                    
        if json_output:
            params['json'] = 'y'
            
        return await call_bart_api('sched', 'stnsched', params)
    except Exception as e:
        logging.error(f"Error fetching station schedule: {e}")
        return {"error": str(e)}

# ======================================PRODUCTION-GRADE API ROUTER======================================

@api_timing_decorator  
async def call_api_for_query(category: str, params: dict, query_text: str, api_endpoint: str = None, websocket = None):
    """
    Production-grade API router that intelligently selects and calls the appropriate BART API endpoint.
    This is the single entry point for all BART API calls from the main application.
    
    Features:
    - Intelligent parameter processing and validation
    - Context-aware parameter enhancement 
    - Automatic date/time normalization
    - Comprehensive error handling
    - Smart endpoint fallback logic
    
    Args:
        category: The query category (ADVISORY, REAL_TIME, ROUTE, SCHEDULE, STATION)
        params: Dictionary of parameters extracted from the query
        query_text: The original query text for context-based decisions
        api_endpoint: Optional specific API endpoint to use (overrides category-based selection)
        websocket: Optional websocket connection for state management
        
    Returns:
        API response data or None if no appropriate API call could be made
    """
    try:
        print(f"\n🚌 [API] ========== PRODUCTION API ROUTER ==========")
        print(f"🚌 [API] Category: {category}")
        print(f"🚌 [API] Endpoint: {api_endpoint}")
        print(f"🚌 [API] Params: {params}")
        print(f"🚌 [API] Query: {query_text}")
        print("==================================================\n")
        
        # ======================================PARAMETER PREPROCESSING======================================
        # Normalize date and time parameters with endpoint awareness
        if "date" in params and params["date"]:
            normalized_date = normalize_date_format(params["date"], api_endpoint)
            if normalized_date:
                params["date"] = normalized_date
                logging.info(f"Normalized date: {params['date']} -> {normalized_date} for endpoint {api_endpoint}")
            else:
                logging.warning(f"Invalid date format: {params['date']}")
                del params["date"]
        
        if "time" in params and params["time"]:
            normalized_time = normalize_time_format(params["time"])
            if normalized_time:
                params["time"] = normalized_time
                logging.info(f"Normalized time: {params['time']} -> {normalized_time}")
            else:
                logging.warning(f"Invalid time format: {params['time']}")
                del params["time"]
        
        # Auto-extract train count for before/after parameters
        if api_endpoint in ["arrive", "depart"]:
            query_lower = query_text.lower()
            train_count = extract_train_count_from_query(query_text)
            
            if train_count is not None:
                if "before" in query_lower:
                    params["b"] = train_count  # Use 'b' for before
                    params["a"] = 0            # Use 'a' for after
                elif "after" in query_lower:
                    params["b"] = 0            # Use 'b' for before
                    params["a"] = train_count  # Use 'a' for after
                else:
                    params["b"] = 0            # Use 'b' for before
                    params["a"] = train_count  # Use 'a' for after
        
        # Auto-extract route color for route endpoints
        if api_endpoint in ["routeinfo", "routesched"] and "route" not in params:
            color = extract_route_color_from_query(query_text)
            if color:
                route_numbers = get_route_from_color(color)
                if route_numbers:
                    params["route"] = route_numbers[0]
                    logging.info(f"Auto-extracted route from color: {color} -> {params['route']}")
        
        # ======================================ENDPOINT ROUTING======================================
        # Route to appropriate API function based on endpoint
        if api_endpoint == "bsa":
            station = params.get("orig") or params.get("station") or params.get("stn") or "all"
            return await get_service_advisories(station=station)
            
        elif api_endpoint == "count":
            return await get_train_count()
            
        elif api_endpoint == "ets":
            eq_type = params.get("eq", "elevator")
            # Auto-detect equipment type from query if not specified
            if "escalator" in query_text.lower():
                eq_type = "escalator"
            elif "elevator" in query_text.lower():
                eq_type = "elevator"
            
            station = params.get("orig") or params.get("station") or params.get("stn")
            return await get_equipment_status(eq=eq_type, stn=station)
            
        elif api_endpoint == "etd":
            station = params.get("orig") or params.get("station") or params.get("stn") or "ALL"
            platform = params.get("plat") or params.get("platform")
            direction = params.get("dir") or params.get("direction")
            gb_color = params.get("gb_color", False)
            return await get_real_time_departures(
                station=station, 
                platform=platform, 
                direction=direction, 
                gb_color=gb_color
            )
            
        elif api_endpoint == "routeinfo":
            route = params.get("route", "1")
            date = params.get("date")
            return await get_route_info(route=route, date=date)
            
        elif api_endpoint == "routes":
            date = params.get("date")
            return await get_all_routes(date=date)
            
        elif api_endpoint == "routesched":
            route = params.get("route")
            date = params.get("date")
            show_legend = params.get("show_legend", False)
            return await get_route_schedule(route=route, date=date, show_legend=show_legend)
            
        elif api_endpoint == "arrive":
            orig = params.get("orig")
            dest = params.get("dest")
            time = params.get("time")
            date = params.get("date")
            before = params.get("before", 0)
            after = params.get("after", 3)
            return await get_schedule_arrivals(
                orig=orig, dest=dest, time=time, date=date, 
                before=before, after=after
            )
            
        elif api_endpoint == "depart":
            orig = params.get("orig")
            dest = params.get("dest")
            time = params.get("time")
            date = params.get("date")
            before = params.get("before", 0)
            after = params.get("after", 3)
            return await get_schedule_departures(
                orig=orig, dest=dest, time=time, date=date,
                before=before, after=after
            )
            
        elif api_endpoint == "fare":
            orig = params.get("orig")
            dest = params.get("dest")
            date = params.get("date")
            sched = params.get("sched")
            return await get_fare_info(orig=orig, dest=dest, date=date, sched=sched)
            
        elif api_endpoint == "stnsched":
            station = params.get("orig") or params.get("station") or params.get("stn")
            date = params.get("date")
            return await get_station_schedule(station=station, date=date)
            
        elif api_endpoint == "scheds":
            return await get_schedules_list()
            
        elif api_endpoint == "stninfo":
            station = params.get("orig") or params.get("station") or params.get("stn")
            return await get_station_info(station=station)
            
        elif api_endpoint == "stns":
            return await get_all_stations()
            
        elif api_endpoint == "stnaccess":
            station = params.get("orig") or params.get("station") or params.get("stn")
            show_legend = params.get("show_legend", False)
            return await get_station_access(station=station, show_legend=show_legend)
            
        else:
            # Fallback logic based on category
            return await _fallback_api_selection(category, params, query_text)
            
    except Exception as e:
        logging.error(f"Error in API router: {str(e)}")
        return {"error": f"API routing error: {str(e)}"}

async def _fallback_api_selection(category: str, params: dict, query_text: str):
    """
    Simplified fallback API selection for edge cases where semantic classification is unclear.
    This provides sensible defaults based on category when specific endpoint detection fails.
    """
    try:
        logging.info(f"🔄 Using fallback selection for category: {category}")
        
        # Simplified fallback logic - most common use case per category
        if category == "ADVISORY":
            # Default to service advisories, with smart equipment detection
            if "elevator" in query_text.lower() or "escalator" in query_text.lower():
                eq_type = "elevator" if "elevator" in query_text.lower() else "escalator"
                station = params.get("orig") or params.get("station") or params.get("stn")
                return await get_equipment_status(eq=eq_type, stn=station)
            elif "count" in query_text.lower() or "many trains" in query_text.lower():
                return await get_train_count()
            else:
                station = params.get("orig") or params.get("station") or params.get("stn") or "all"
                return await get_service_advisories(station=station)
                
        elif category == "REAL_TIME":
            # Default to real-time departures
            station = params.get("orig") or params.get("station") or params.get("stn") or "ALL"
            return await get_real_time_departures(station=station)
            
        elif category == "ROUTE":
            # Default to specific route info if route specified, otherwise all routes
            if params.get("route"):
                return await get_route_info(route=params["route"])
            else:
                return await get_all_routes()
                
        elif category == "SCHEDULE":
            # Default to departure schedules for two stations, station schedule for one
            if params.get("orig") and params.get("dest"):
                return await get_schedule_departures(orig=params["orig"], dest=params["dest"])
            else:
                station = params.get("orig") or params.get("station") or params.get("stn")
                if station:
                    return await get_station_schedule(station=station)
                else:
                    return await get_schedules_list()
                
        elif category == "STATION":
            # Default to station info, with access info for specific queries
            station = params.get("orig") or params.get("station") or params.get("stn")
            if station:
                if any(word in query_text.lower() for word in ["parking", "bike", "access", "accessibility"]):
                    return await get_station_access(station=station)
                else:
                    return await get_station_info(station=station)
            else:
                return await get_all_stations()
        
        # If no category matches, return error
        logging.warning(f"⚠️ No fallback available for category: {category}")
        return {"error": f"No appropriate API endpoint found for category: {category}"}
        
    except Exception as e:
        logging.error(f"❌ Error in fallback API selection: {str(e)}")
        return {"error": f"Fallback API selection error: {str(e)}"}