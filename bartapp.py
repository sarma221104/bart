#======================================IMPORTS======================================
import asyncio
import json
import re
import boto3
import logging
import requests
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
from datetime import datetime
import uuid
import pytz
import collections
import traceback
import os
#======================================IMPORT PAGES======================================
from prompts import (
    BART_ASSISTANT_PROMPT,
    INTENT_CLASSIFICATION_PROMPT,
    QUERY_TYPE_CLASSIFICATION_PROMPT,
    COMBINED_RESPONSE_PROMPT,
    KB_INSTRUCTIONS,
    PROMPT1,
    PROMPT2,
    PROMPT3,
    PROMPT4
)
from apis import (
    get_service_advisories,get_train_count,get_equipment_status,
    get_real_time_departures,
    get_station_info,get_all_stations,get_station_access,
    get_route_info,get_all_routes,get_route_schedule,
    get_schedules_list,get_fare_info,get_schedule_arrivals,get_schedule_departures,get_station_schedule
)
from constants import ( station_data, station_groups, supported_languages, thinking_messages )
# ======================================FASTAPI SETUP======================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
# ======================================AWS CLIENTS======================================
bedrock_client = boto3.client("bedrock-runtime", region_name="us-west-2")
kb_client = boto3.client("bedrock-agent-runtime", region_name="us-west-2")
polly_client = boto3.client("polly", region_name="us-west-2")
dynamodb = boto3.resource('dynamodb', region_name="us-west-2")

# ======================================DYNAMODB TABLES======================================
try:
    bart_table = dynamodb.Table('BartData')
    logging.info("Got reference to BartData table")
except Exception as e:
    logging.error(f"Error getting DynamoDB table reference: {str(e)}")
def handle_dynamodb_error(operation_name: str, error: Exception):
    """Handle DynamoDB errors gracefully"""
    if "AccessDeniedException" in str(error):
        logging.error(f"Access denied for {operation_name}. Please check IAM permissions.")
    elif "ResourceNotFoundException" in str(error):
        logging.error(f"Table not found for {operation_name}. Please create the table first.")
    else:
        logging.error(f"Error in {operation_name}: {str(error)}")
    return None
# ======================================CONSTANTS======================================
KNOWLEDGE_BASE_ID = "J420HVM1JE"
CHUNK_SIZE = 64000
REGION = "us-west-2"
MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/{MODEL_ID}"
SUPPORTED_LANGUAGES = supported_languages
prompt_template = BART_ASSISTANT_PROMPT
# ======================================BART API CONFIGURATION======================================
BART_API_KEY = 'QW8P-55EA-9NVT-DWEI'
BART_BASE_URL = 'https://dev-api.bart.gov/api'
# ======================================LOGGING SETUP======================================
logging.basicConfig(level=logging.INFO)
def truncate_json_for_logging(json_data, max_length=500):
    """Truncate JSON data for more concise logging"""
    if json_data is None:
        return "None"
    try:
        if isinstance(json_data, (dict, list)):
            json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
            if len(json_str) > max_length:
                return json_str[:max_length] + "... [truncated]"
            return json_str
        else:
            str_data = str(json_data)
            if len(str_data) > max_length:
                return str_data[:max_length] + "... [truncated]"
            return str_data
    except:
        return str(json_data)[:max_length] + "... [truncated]"
# ======================================STATION DATA======================================
STATION_DATA = station_data
STATION_GROUPS = station_groups
# ======================================HEALTH CHECK ENDPOINT======================================
@app.get("/api/health")
async def health_check(check_type: str = "all"):
    """
    Health check endpoint that returns a 200 OK status if healthy, 503 if unhealthy.
    Tests text and/or audio websocket endpoints to ensure they're working properly.
    Args:
        check_type: Type of check to perform - "all", "text", or "audio"
    Returns:
        Response with status code 200 if healthy, 503 if unhealthy, 400 for invalid check_type
    """
    try:
        text_status = "not_checked"
        audio_status = "not_checked"
        host = os.environ.get('HOST', 'localhost')
        port = os.environ.get('PORT', 8000)
        
        if check_type.lower() in ["all", "text"]:
            text_status = "healthy"
            try:
                async with websockets.connect(f"ws://{host}:{port}/ws/audio?skip_tts=true&health_check=true") as ws:
                    init_data = {
                        "user_id": "health_check_user",
                        "email": "health_check@example.com",
                        "session_id": "health_check_session",
                        "language": "en",
                        "skip_tts": True,
                        "health_check": True
                    }
                    await ws.send(json.dumps(init_data, ensure_ascii=False))
                    test_query = "How many trains are currently active in the system?"
                    await ws.send(test_query)
                    try:
                        response = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        if not response:
                            text_status = "unhealthy"
                        logging.info(f"Text websocket health check successful: {response[:50]}...")
                    except asyncio.TimeoutError:
                        logging.error("Health check timed out waiting for response")
                        text_status = "unhealthy"
                    except websockets.exceptions.ConnectionClosedOK:
                        logging.info("WebSocket closed gracefully during health check")
                    except websockets.exceptions.ConnectionClosedError as e:
                        logging.warning(f"WebSocket connection closed with error during health check: {e}")
                        text_status = "unhealthy"
            except Exception as e:
                logging.error(f"Health check failed for text websocket: {str(e)}")
                text_status = "unhealthy"
        if check_type.lower() in ["all", "audio"]:
            audio_status = "healthy"
            try:
                async with websockets.connect(f"ws://{host}:{port}/ws/audio?health_check=true") as ws:
                    init_data = {
                        "user_id": "health_check_user",
                        "email": "health_check@example.com",
                        "session_id": "health_check_session",
                        "language": "en",
                        "health_check": True
                    }
                    await ws.send(json.dumps(init_data, ensure_ascii=False))
                    logging.info("Audio websocket connection established successfully")
                    try:
                        await asyncio.sleep(0.5)
                        await ws.close(code=1000, reason="Health check completed")
                    except websockets.exceptions.ConnectionClosedOK:
                        logging.info("WebSocket closed gracefully during audio health check")
                    except websockets.exceptions.ConnectionClosedError as e:
                        logging.warning(f"WebSocket connection closed with error during audio health check: {e}")
            except Exception as e:
                logging.error(f"Health check failed for audio websocket: {str(e)}")
                audio_status = "unhealthy"
        
        # Determine overall status based on what was checked
        if check_type.lower() == "all":
            overall_status = "healthy" if text_status == "healthy" and audio_status == "healthy" else "unhealthy"
        elif check_type.lower() == "text":
            overall_status = text_status
        elif check_type.lower() == "audio":
            overall_status = audio_status
        else:
            print("Returning status code 400 for invalid check_type")
            return Response(status_code=400)
        status_code = 200 if overall_status == "healthy" else 503
        print(f"Health check status code: {status_code}")
        return Response(status_code=status_code)
    except Exception as e:
        logging.error(f"Health check failed: {str(e)}")
        print("Returning status code 503 due to exception")
        return Response(status_code=503)
# ======================================STATION HELPER FUNCTIONS======================================
def get_station_code(station_name: str) -> Optional[str]:
    """Get station code from station name or abbreviation using in-memory station data"""
    try:
        if not station_name:
            return None
        station_name_lower = station_name.lower().strip()
        sfo_aliases = ["sfo", "sfo airport", "san francisco airport", "sf airport", 
                       "san francisco international", "san francisco international airport"]
        for alias in sfo_aliases:
            if station_name_lower == alias or re.search(r'\b' + re.escape(alias) + r'\b', station_name_lower):
                for station in STATION_DATA['stations']:
                    if station['abbr'].lower() == "sfia":
                        logging.info(f"Found SFO airport match: {station['name']} -> {station['abbr']}")
                        return station['abbr']
        for suffix in [" station", " bart station", " bart"]:
            if station_name_lower.endswith(suffix):
                station_name_lower = station_name_lower[:-len(suffix)].strip()
                break
        logging.info(f"Looking up station code for: {station_name_lower}")
        for station in STATION_DATA['stations']:
            if station['abbr'].lower() == station_name_lower:
                logging.info(f"Found exact abbreviation match: {station['abbr']}")
                return station['abbr']
        for station in STATION_DATA['stations']:
            if station['name'].lower() == station_name_lower:
                logging.info(f"Found exact name match: {station['name']} -> {station['abbr']}")
                return station['abbr']
        for station in STATION_DATA['stations']:
            if '/' in station['name']:
                parts = [part.strip().lower() for part in station['name'].split('/')]
                if station_name_lower in parts:
                    logging.info(f"Found match with part of station name: {station['name']} -> {station['abbr']}")
                    return station['abbr']
        station_name_no_spaces = re.sub(r'\s+', '', station_name_lower)
        for station in STATION_DATA['stations']:
            station_no_spaces = re.sub(r'\s+', '', station['name'].lower())
            if station_no_spaces == station_name_no_spaces:
                logging.info(f"Found match after removing spaces: {station['name']} -> {station['abbr']}")
                return station['abbr']
        is_ambiguous, options = check_station_ambiguity(station_name_lower)
        if is_ambiguous:
            return None, options
        for station in STATION_DATA['stations']:
            if station_name_lower in station['name'].lower():
                logging.info(f"Found partial name match: {station['name']} -> {station['abbr']}")
                return station['abbr']
        logging.warning(f"No station code found for: {station_name}")
        return None
    except Exception as e:
        logging.error(f"Error getting station code: {str(e)}")
        return None

def check_station_ambiguity(station_name: str) -> Tuple[bool, List[str]]:
    """Check if a station name is ambiguous and return options if it is"""
    options = []
    station_name_lower = station_name.lower().strip()
    
    if len(station_name_lower) < 3:
        return False, []
    sfo_aliases = ["sfo", "sfo airport", "san francisco airport", "sf airport", 
                   "san francisco international", "san francisco international airport"]
    if any(re.search(r'\b' + re.escape(alias) + r'\b', station_name_lower) for alias in sfo_aliases) or station_name_lower in sfo_aliases:
        print(f"Found SFO airport match for '{station_name}', not treating as ambiguous")
        return False, []
    for station in STATION_DATA['stations']:
        if station['name'].lower() == station_name_lower:
            print(f"Exact station match found for '{station_name}', not treating as ambiguous")
            return False, []
    for station in STATION_DATA['stations']:
        if '/' in station['name']:
            parts = [part.strip().lower() for part in station['name'].split('/')]
            if station_name_lower in parts:
                print(f"Exact match with a part of station '{station['name']}', not treating as ambiguous")
                return False, [station['name']]
    for group_key, stations_list in STATION_GROUPS.items():
        if group_key.lower() == station_name_lower or group_key.lower() in station_name_lower or station_name_lower in group_key.lower():
            options = stations_list.copy()
            print(f"Found station group match: '{group_key}' -> {options}")
            break
    
    if not options:
        matching_stations = []
        for station in STATION_DATA['stations']:
            if station_name_lower in station['name'].lower():
                matching_stations.append(station['name'])
                continue
            station_parts = station['name'].lower().split()
            if any(part in station_name_lower for part in station_parts if len(part) > 3):
                matching_stations.append(station['name'])
                continue
            if '/' in station['name']:
                slash_parts = [part.strip().lower() for part in station['name'].split('/')]
                if any(part in station_name_lower for part in slash_parts):
                    matching_stations.append(station['name'])
                    continue
        if len(matching_stations) > 1:
            options = matching_stations
            print(f"Found multiple matching stations: {options}")
    
    print(f"Ambiguity check for '{station_name}': {'Ambiguous' if len(options) > 1 else 'Not ambiguous'}, Options: {options}")
    return len(options) > 1, options

def normalize_station_name(station_name: str) -> str:
    """
    Cleans and normalizes a station name by removing/replacing punctuation
    and matching it to a valid BART station if possible.
    
    Args:
        station_name: The station name to normalize
        
    Returns:
        Normalized station name that matches a valid BART station,
        or the original name if no match was found
    """
    if not station_name:
        return station_name
    sfo_aliases = ["sfo", "sfo airport", "san francisco airport", "sf airport", 
                  "san francisco international", "san francisco international airport"]
    clean_name = re.sub(r'[,;:]', ' ', station_name)
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()
    clean_name_lower = clean_name.lower()
    if any(re.search(r'\b' + re.escape(alias) + r'\b', clean_name_lower) for alias in sfo_aliases) or clean_name_lower in sfo_aliases:
        for station in STATION_DATA["stations"]:
            if station["abbr"].lower() == "sfia":
                return station["name"]
    for station in STATION_DATA["stations"]:
        if station["name"].lower() == clean_name_lower:
            return station["name"]
    for station in STATION_DATA["stations"]:
        if station["abbr"].lower() == clean_name_lower:
            return station["name"]
    clean_name_no_punct = re.sub(r'[^\w\s]', '', clean_name).strip()
    clean_name_no_punct_lower = clean_name_no_punct.lower()
    
    for station in STATION_DATA["stations"]:
        station_no_punct = re.sub(r'[^\w\s]', '', station["name"]).strip()
        if station_no_punct.lower() == clean_name_no_punct_lower:
            return station["name"]
    for station in STATION_DATA["stations"]:
        station_compressed = re.sub(r'\s+', '', station["name"].lower())
        input_compressed = re.sub(r'\s+', '', clean_name_lower)
        if station_compressed == input_compressed:
            return station["name"]
    return clean_name
# ======================================BART API CALL FUNCTION======================================
async def call_bart_api(endpoint: str, cmd: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Make a call to the BART API with the correct format"""
    try:
        print("\n========== BART API REQUEST ==========")
        print(f"Endpoint: {endpoint}")
        print(f"Command: {cmd}")
        print(f"Parameters: {json.dumps(params, indent=2, ensure_ascii=False) if params else 'None'}")
        print("======================================\n")
        if params is None:
            params = {}
        new_params = {}
        station_params = []
        for key, value in params.items():
            if value is None:
                continue
                
            if key in ['stn', 'orig', 'dest'] and value:
                station_params.append(str(value))
        if station_params:
            validation_result = validate_stations(station_params)
            if not validation_result["valid"]:
                invalid_stations = validation_result["invalid_stations"]
                invalid_station_str = ", ".join(invalid_stations)
                raise ValueError(f"Found invalid station(s): {invalid_station_str}")
        for key, value in params.items():
            if value is None:
                continue
            if key == 'stn':
                if endpoint == 'ets':
                    station_code = get_station_code(str(value))
                    if isinstance(station_code, tuple):
                        raise ValueError(f"Ambiguous station: {value}. Please specify which station you mean.")
                    if station_code:
                        new_params['stn'] = station_code
                    else:
                        raise ValueError(f"Invalid station: {value}")
                else:
                    station_code = get_station_code(str(value))
                    if isinstance(station_code, tuple):
                        raise ValueError(f"Ambiguous station: {value}. Please specify which station you mean.")
                    if station_code:
                        new_params['orig'] = station_code
                    else:
                        raise ValueError(f"Invalid station: {value}")
            elif key in ['orig', 'dest']:
                if str(value).upper() == "ALL":
                    new_params[key] = "ALL"
                else:
                    station_code = get_station_code(str(value))
                    if isinstance(station_code, tuple):
                        raise ValueError(f"Ambiguous {key} station: {value}. Please specify which station you mean.")
                    if station_code:
                        new_params[key] = station_code
                    else:
                        raise ValueError(f"Invalid {key} station: {value}")
            else:
                new_params[key] = value
        url = f"{BART_BASE_URL}/{endpoint}.aspx"
        query_string = f"cmd={cmd}&key={BART_API_KEY}"
        if 'json' not in new_params:
            query_string += "&json=y"
        for key, value in new_params.items():
            query_string += f"&{key}={value}"
        
        full_url = f"{url}?{query_string}"
        logging.info(f"Making BART API call: {full_url}")
        response = requests.get(full_url)
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
        
        logging.info(f"BART API response: {truncate_json_for_logging(result, max_length=300)}")
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response format: {type(result)}")
        return result
    except requests.exceptions.RequestException as re:
        error_msg = f"Network error calling BART API: {str(re)}"
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

async def call_api_for_query(category: str, params: dict, query_text: str, api_endpoint: str = None, websocket = None):
    """
    Single entry point for making BART API calls based on query category and parameters.
    This consolidates all API calling logic in one place for better maintainability.
    Args:
        category: The query category (ADVISORY, REAL_TIME, ROUTE, SCHEDULE, STATION)
        params: Dictionary of parameters extracted from the query
        query_text: The original query text for context-based decisions
        api_endpoint: Optional specific API endpoint to use (overrides category-based selection)
        websocket: Optional websocket connection to access state information
    Returns:
        API response data or None if no appropriate API call could be made
    """
    query_lower = query_text.lower()
    departure_keywords = ["depart", "departing", "departure", "leaving", "leave", "next train"]
    arrival_keywords = ["arrive", "arriving", "arrival", "coming", "incoming", "next train"]
    has_departure_keyword = any(keyword in query_lower for keyword in departure_keywords)
    has_arrival_keyword = any(keyword in query_lower for keyword in arrival_keywords)
    station_names = extract_station_names(query_text)
    if (has_departure_keyword or has_arrival_keyword):
        unique_stations = []
        station_mapping = {}
        for station in station_names:
            is_duplicate = False
            for existing_station in unique_stations:
                if '/' in existing_station and station.lower() in existing_station.lower().split('/'):
                    is_duplicate = True
                    station_mapping[station] = existing_station
                    print(f"Detected {station} as part of {existing_station}, treating as the same station")
                    break
                elif '/' in station and existing_station.lower() in station.lower().split('/'):
                    is_duplicate = True
                    station_mapping[existing_station] = station
                    unique_stations.remove(existing_station)
                    unique_stations.append(station)
                    print(f"Detected {existing_station} as part of {station}, treating as the same station")
                    break
            if not is_duplicate:
                unique_stations.append(station)
        print(f"Original station names: {station_names}")
        print(f"Unique station names after deduplication: {unique_stations}")
        if len(unique_stations) == 1:
            if category != "REAL_TIME" or api_endpoint != "etd":
                print(f"\n========== OVERRIDING API ENDPOINT ==========")
                print(f"Found single unique station ({unique_stations[0]}) with arrival/departure keywords")
                print(f"Overriding category from {category} to REAL_TIME")
                print(f"Overriding endpoint from {api_endpoint} to etd")
                print("==============================================\n")
                
                category = "REAL_TIME"
                api_endpoint = "etd"
                full_station_name = unique_stations[0]
                params["station"] = full_station_name
                if "orig" in params:
                    del params["orig"]
                if "dest" in params:
                    del params["dest"]
        elif len(unique_stations) == 2 and category == "REAL_TIME":
            print(f"\n========== CORRECTING API ENDPOINT ==========")
            print(f"Found two unique stations ({unique_stations}) with arrival/departure keywords")
            print(f"Correcting category from REAL_TIME to SCHEDULE")
            
            category = "SCHEDULE"
            if has_arrival_keyword and not has_departure_keyword:
                api_endpoint = "arrive"
                print(f"Setting endpoint to arrive based on arrival keywords")
            else:
                api_endpoint = "depart"
                print(f"Setting endpoint to depart based on departure keywords")
            print("==============================================\n")
            if "orig" not in params and "dest" not in params:
                params["orig"] = unique_stations[0]
                params["dest"] = unique_stations[1]
        elif len(unique_stations) == 2:
            print(f"\n========== KEEPING SCHEDULE API ENDPOINT ==========")
            print(f"Found exactly two unique stations in query: {unique_stations}")
            print(f"Using {api_endpoint} endpoint as classified")
            print("=================================================\n")
        elif 'orig' in params and 'dest' in params and params['orig'] and params['dest']:
            orig = params['orig']
            dest = params['dest']
            orig_normalized = normalize_station_name(orig).lower()
            dest_normalized = normalize_station_name(dest).lower()
            
            if orig_normalized == dest_normalized:
                print(f"\n========== CORRECTING PARAMETERS ==========")
                print(f"Origin ({orig}) and destination ({dest}) are the same station")
                print(f"Switching to REAL_TIME with etd endpoint")
                print("===========================================\n")
                
                category = "REAL_TIME"
                api_endpoint = "etd"
                params["station"] = orig
                del params["orig"]
                del params["dest"]
            else:
                is_same_station = False
                for station in STATION_DATA["stations"]:
                    if '/' in station["name"]:
                        parts = [part.strip().lower() for part in station["name"].split('/')]
                        if orig_normalized in parts and dest_normalized in parts:
                            is_same_station = True
                            print(f"Origin ({orig}) and destination ({dest}) are parts of the same station: {station['name']}")
                            category = "REAL_TIME"
                            api_endpoint = "etd"
                            params["station"] = station["name"]
                            del params["orig"]
                            del params["dest"]
                            break
                
                if not is_same_station:
                    print(f"\n========== KEEPING SCHEDULE API ENDPOINT ==========")
                    print(f"Found both origin ({params['orig']}) and destination ({params['dest']}) stations")
                    print(f"Using {api_endpoint} endpoint as classified")
                    print("=================================================\n")
    
    station_params_to_validate = []
    for key, value in params.items():
        if key in ['orig', 'dest', 'station', 'stn'] and value:
            station_params_to_validate.append(str(value))
    if station_params_to_validate:
        print(f"\n========== VALIDATING STATION PARAMETERS ==========")
        print(f"Station parameters to validate: {station_params_to_validate}")
        validation_result = validate_stations(station_params_to_validate)
        if validation_result["params"]:
            for key, value in validation_result["params"].items():
                for param_key, param_value in list(params.items()):
                    if param_key in ['orig', 'dest', 'station', 'stn'] and param_value and param_value.lower() == key.lower():
                        print(f"Replacing parameter {param_key}: '{param_value}' → '{value}'")
                        params[param_key] = value
        if not validation_result["valid"]:
            print(f"Found invalid stations: {validation_result['invalid_stations']}")
            if websocket:
                try:
                    updated_query = await process_station_validation(
                        query_text, 
                        validation_result["invalid_stations"],
                        validation_result["suggestions"],
                        websocket
                    )
                    
                    if not updated_query:
                        print("User has been prompted for station validation, waiting for response...")
                        return None
                    
                    print(f"Updated query after station validation: {updated_query}")
                    
                    replaced_stations = {}
                    for invalid_station in validation_result["invalid_stations"]:
                        for station in STATION_DATA["stations"]:
                            station_name = station["name"]
                            station_pattern = r'\b' + re.escape(station_name) + r'\b'
                            if (re.search(station_pattern, updated_query, re.IGNORECASE) and 
                                not re.search(station_pattern, query_text, re.IGNORECASE)):
                                replaced_stations[invalid_station] = station_name
                                print(f"Station replacement identified: '{invalid_station}' → '{station_name}'")
                                break
                            abbr_pattern = r'\b' + re.escape(station["abbr"]) + r'\b'
                            if (re.search(abbr_pattern, updated_query, re.IGNORECASE) and 
                                not re.search(abbr_pattern, query_text, re.IGNORECASE)):
                                replaced_stations[invalid_station] = station_name
                                print(f"Station replacement identified (by abbr): '{invalid_station}' → '{station_name}'")
                                break
                    if replaced_stations:
                        for key, value in list(params.items()):
                            if key in ['orig', 'dest', 'station', 'stn'] and value in replaced_stations:
                                params[key] = replaced_stations[value]
                                print(f"Updated parameter {key}: '{value}' → '{replaced_stations[value]}'")
                    else:
                        for invalid_station in validation_result["invalid_stations"]:
                            for key, value in list(params.items()):
                                if key in ['orig', 'dest', 'station', 'stn'] and value == invalid_station:
                                    for station in STATION_DATA["stations"]:
                                        station_name = station["name"]
                                        if station_name.lower() in updated_query.lower():
                                            params[key] = station_name
                                            print(f"Updated parameter {key} from '{invalid_station}' to '{station_name}' (fallback method)")
                                            break
                except Exception as e:
                    print(f"Error during station validation: {str(e)}")
                    traceback.print_exc()
                    return {"error": f"Error validating stations: {str(e)}"}
            else:
                return {"error": f"Invalid station(s): {', '.join(validation_result['invalid_stations'])}"}
    date_type = extract_date_type_from_query(query_text)
    if date_type and "date" not in params:
        print(f"Extracted date type '{date_type}' from query: '{query_text}'")
        params["date"] = date_type
    if "date" in params:
        normalized_date = normalize_date_format(params["date"])
        if normalized_date:
            params["date"] = normalized_date
        else:
            print(f"Invalid date format: {params['date']}")
            del params["date"]
    
    if "time" in params:
        normalized_time = normalize_time_format(params["time"])
        if normalized_time:
            params["time"] = normalized_time
        else:
            print(f"Invalid time format: {params['time']}")
            del params["time"]
    try:
        if 'orig' in params and 'dest' in params and 'station' in params:
            print("\n========== CLEANING PARAMETERS ==========")
            print(f"Found both orig/dest and station parameters - removing station parameter")
            print(f"Before: {json.dumps(params, indent=2, ensure_ascii=False)}")
            del params['station']
            print(f"After: {json.dumps(params, indent=2, ensure_ascii=False)}")
            print("=======================================\n")
        
        print("\n========== CONSOLIDATED API CALL ==========")
        print(f"Category: {category}")
        print(f"Suggested endpoint: {api_endpoint}")
        print(f"Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}")
        print("==========================================\n")
        
        is_complete_query = False
        origin_station = None
        destination_station = None
        
        if websocket:
            if 'orig' in params and params['orig']:
                origin_station = params['orig']
                if not getattr(websocket.state, 'awaiting_location', False):
                    websocket.state.origin_station = origin_station
                    
            if 'dest' in params and params['dest']:
                destination_station = params['dest']
                if not getattr(websocket.state, 'awaiting_location', False):
                    websocket.state.destination_station = destination_station
                    
            if getattr(websocket.state, 'complete_query', False):
                try:
                    is_complete_query = True
                    if not origin_station:
                        origin_station = getattr(websocket.state, 'origin_station', None)
                    if not destination_station:
                        destination_station = getattr(websocket.state, 'destination_station', None)
                except AttributeError:
                    is_complete_query = False
        
        print("\n========== QUERY ANALYSIS ==========")
        print(f"Is complete query: {is_complete_query}")
        print(f"Origin station: {origin_station}")
        print(f"Destination station: {destination_station}")
        print(f"Has origin/dest: {bool('orig' in params and 'dest' in params)}")
        print(f"Has station: {bool('station' in params)}")
        print(f"Is 'to' query: {'to ' in query_text.lower()}")
        print(f"Is 'from' query: {'from ' in query_text.lower()}")
        print("===================================\n")
        if api_endpoint:
            print(f"Using provided API endpoint from classification: {api_endpoint}")
            #---------------------------#
            # ADVISORY CATEGORY ENDPOINTS
            #---------------------------#
            if api_endpoint == "bsa":
                # Required: cmd=bsa
                # Optional: orig (station)
                station_param = params.get("station", "all")
                station_code = get_station_code(station_param) if station_param and station_param.lower() != "all" else "all"
                
                api_params = {'orig': station_code}
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                print(f"Making call to service advisories endpoint for station: {station_param or 'all'}")
                return await call_bart_api('bsa', 'bsa', api_params)
            elif api_endpoint == "count":
                # Required: cmd=count (no other parameters)
                api_params = {}
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                print("Making call to train count endpoint")
                return await call_bart_api('bsa', 'count', api_params)
            
            elif api_endpoint == "ets":
                # Required: eq=elevator/escalator
                # Optional: stn=station_code
                eq_type = params.get('eq', 'elevator').lower()
                
                if 'escalator' in query_text.lower():
                    eq_type = 'escalator'
                elif 'elevator' in query_text.lower():
                    eq_type = 'elevator'
                
                if eq_type not in ['elevator', 'escalator']:
                    eq_type = 'elevator' 
                
                api_params = {
                    'eq': eq_type
                }
                
                station_param = None
                if 'station' in params:
                    station_param = params['station']
                elif 'stn' in params:
                    station_param = params['stn']
                
                if station_param:
                    station_code = get_station_code(station_param)
                    if isinstance(station_code, str):
                        api_params['stn'] = station_code
                   
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                
                print(f"Making call to equipment status endpoint for {api_params['eq']} at station: {station_param or 'all stations'}")
                return await call_bart_api('ets', 'status', api_params)
            #---------------------------#
            # REAL_TIME CATEGORY ENDPOINTS
            #---------------------------#
            elif api_endpoint == "etd":
                # Required: cmd=etd, orig
                # Optional: platform, direction, gb_color, json_output
                station_param = params.get("station", "ALL")
                station_code = get_station_code(station_param) if station_param and station_param.lower() != "all" else "ALL"
                api_params = {'orig': station_code}
                if "platform" in params:
                    if not params["platform"].isdigit() or not (1 <= int(params["platform"]) <= 4):
                        print("Platform must be a number between 1 and 4")
                        return None
                    api_params['plat'] = params["platform"]
                if "direction" in params:
                    direction = params["direction"].lower()
                    if direction not in ['n', 's']:
                        print("Direction must be 'n' for Northbound or 's' for Southbound")
                        return None
                    api_params['dir'] = direction
                if params.get("gb_color", False):
                    api_params['gbColor'] = '1'
                    
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                    
                print(f"Making call to real-time departures endpoint for station: {station_param}")
                return await call_bart_api('etd', 'etd', api_params)
            #---------------------------#
            # ROUTE CATEGORY ENDPOINTS
            #---------------------------#
            elif api_endpoint == "routeinfo":
                # Required: cmd=routeinfo, route
                route = params.get("route", "1")
                if not (route.isdigit() and 1 <= int(route) <= 12) and route.lower() != 'all':
                    route_numbers = get_route_from_color(route)
                    if not route_numbers:
                        color = extract_route_color_from_query(query_text)
                        if color:
                            route_numbers = get_route_from_color(color)
                    if route_numbers:
                        route = route_numbers[0]
                        logging.info(f"Mapped color to route number '{route}' for API call")
                    elif route.lower() != 'all':
                        logging.warning(f"Invalid route parameter: {route}")
                        return {"error": "Route must be a number between 1 and 12,19,20 'all', or a valid line color (Yellow, Orange, Green, Red, Blue, Grey)"}
                
                print(f"Making call to route info endpoint for route: {route}")
                return await call_bart_api('route', 'routeinfo', {'route': route})
            
            elif api_endpoint == "routes":
                # Required: cmd=routes (no other parameters)
                print("Making call to routes list endpoint")
                return await call_bart_api('route', 'routes', {})
            #---------------------------#
            # SCHEDULE CATEGORY ENDPOINTS
            #---------------------------#
            elif api_endpoint == "arrive":
                if 'orig' in params and 'dest' in params and params['orig'] and params['dest']:
                    print(f"\n========== USING SCHEDULE API ENDPOINT ==========")
                    print(f"Found both origin ({params['orig']}) and destination ({params['dest']}) stations")
                    print(f"Using arrive endpoint as classified")
                    print("=================================================\n")
                else:
                    station_names = extract_station_names(query_text)
                    if len(station_names) == 1:
                        print(f"\n========== REDIRECTING TO ETD ENDPOINT ==========")
                        print(f"Found single station ({station_names[0]}) with arrival keywords")
                        print(f"Redirecting from arrive to etd endpoint")
                        print("================================================\n")
                        
                        station_param = station_names[0]
                        station_code = get_station_code(station_param)
                        
                        if isinstance(station_code, str):
                            api_params = {'orig': station_code}
                            if params.get("json_output", True):
                                api_params['json'] = 'y'
                                
                            print(f"Making call to real-time departures endpoint for station: {station_param}")
                            return await call_bart_api('etd', 'etd', api_params)
                
                # Required: cmd=arrive, orig, dest
                # Optional: time, date, before, after
                orig = params.get("orig")
                dest = params.get("dest")
                
                if is_complete_query and origin_station and destination_station:
                    orig = origin_station
                    dest = destination_station
                
                if not orig or not dest:
                    print("Missing origin or destination parameters")
                    return None
                    
                orig_code = get_station_code(orig)
                dest_code = get_station_code(dest)
                
                if not isinstance(orig_code, str) or not isinstance(dest_code, str):
                    print(f"Invalid station codes: origin={orig_code}, dest={dest_code}")
                    return None
                
                api_params = {
                    'orig': orig_code,
                    'dest': dest_code,
                }
                
                query_lower = query_text.lower()
                
                before_val = 0
                after_val = 3
                
                if "before" in query_lower:
                    before_val = 3
                    after_val = 0
                elif "after" in query_lower:
                    before_val = 0
                    after_val = 3
                
                train_count = extract_train_count_from_query(query_text)
                if train_count is not None:
                    if "before" in query_lower:
                        before_val = train_count
                        after_val = 0
                    elif "after" in query_lower:
                        before_val = 0
                        after_val = train_count
                    else:
                        before_val = 0
                        after_val = train_count
                
                api_params['b'] = str(after_val)
                api_params['a'] = str(before_val)
                
                if "time" in params:
                    normalized_time = normalize_time_format(params["time"])
                    if normalized_time:
                        api_params['time'] = normalized_time
                
                if "date" in params:
                    api_params['date'] = params["date"]
                
                print(f"Making call to arrivals endpoint from {orig} to {dest}")
                return await call_bart_api('sched', 'arrive', api_params)
            
            elif api_endpoint == "depart":
                if 'orig' in params and 'dest' in params and params['orig'] and params['dest']:
                    print(f"\n========== USING SCHEDULE API ENDPOINT ==========")
                    print(f"Found both origin ({params['orig']}) and destination ({params['dest']}) stations")
                    print(f"Using depart endpoint as classified")
                    print("=================================================\n")
                else:
                    station_names = extract_station_names(query_text)
                    if len(station_names) == 1:
                        print(f"\n========== REDIRECTING TO ETD ENDPOINT ==========")
                        print(f"Found single station ({station_names[0]}) with departure keywords")
                        print(f"Redirecting from depart to etd endpoint")
                        print("================================================\n")
                        
                        station_param = station_names[0]
                        station_code = get_station_code(station_param)
                        
                        if isinstance(station_code, str):
                            api_params = {'orig': station_code}
                            if params.get("json_output", True):
                                api_params['json'] = 'y'
                                
                            print(f"Making call to real-time departures endpoint for station: {station_param}")
                            return await call_bart_api('etd', 'etd', api_params)
                
                # Required: cmd=depart, orig, dest
                # Optional: time, date, before, after
                orig = params.get("orig")
                dest = params.get("dest")
                
                if is_complete_query and origin_station and destination_station:
                    orig = origin_station
                    dest = destination_station
                
                if not orig or not dest:
                    print("Missing origin or destination parameters")
                    return None
                    
                orig_code = get_station_code(orig)
                dest_code = get_station_code(dest)
                
                if not isinstance(orig_code, str) or not isinstance(dest_code, str):
                    print(f"Invalid station codes: origin={orig_code}, dest={dest_code}")
                    return None
                
                api_params = {
                    'orig': orig_code,
                    'dest': dest_code,
                }
                
                query_lower = query_text.lower()
                
                before_val = 0
                after_val = 3
                
                if "before" in query_lower:
                    before_val = 3
                    after_val = 0
                elif "after" in query_lower:
                    before_val = 0
                    after_val = 3
                
                train_count = extract_train_count_from_query(query_text)
                if train_count is not None:
                    if "before" in query_lower:
                        before_val = train_count
                        after_val = 0
                    elif "after" in query_lower:
                        before_val = 0
                        after_val = train_count
                    else:
                        before_val = 0
                        after_val = train_count
                
                api_params['b'] = str(before_val)
                api_params['a'] = str(after_val)
                
                if "time" in params:
                    normalized_time = normalize_time_format(params["time"])
                    if normalized_time:
                        api_params['time'] = normalized_time
                
                if "date" in params:
                    api_params['date'] = params["date"]
                
                print(f"Making call to departures endpoint from {orig} to {dest}")
                return await call_bart_api('sched', 'depart', api_params)
            
            elif api_endpoint == "fare":
                # Required: cmd=fare, orig, dest
                orig = params.get("orig")
                dest = params.get("dest")
                
                if not orig or not dest:
                    print("Missing origin or destination parameters")
                    return None
                    
                orig_code = get_station_code(orig)
                dest_code = get_station_code(dest)
                
                if not isinstance(orig_code, str) or not isinstance(dest_code, str):
                    print(f"Invalid station codes: origin={orig_code}, dest={dest_code}")
                    return None
                
                print(f"Making call to fare endpoint from {orig} to {dest}")
                return await call_bart_api('sched', 'fare', {'orig': orig_code, 'dest': dest_code})
            
            elif api_endpoint == "stnsched":
                station_param = params.get("station") or params.get("orig")
                if not station_param:
                    print("Missing station parameter")
                    return None
                    
                station_code = get_station_code(station_param)
                if not isinstance(station_code, str):
                    print(f"Invalid station code: {station_code}")
                    return None
                
                api_params = {'orig': station_code}

                if "date" in params:
                    normalized_date = normalize_date_format(params["date"])
                    if normalized_date:
                        api_params['date'] = normalized_date
                else:
                    date_type = extract_date_type_from_query(query_text)
                    if date_type:
                        if date_type in ['wd', 'sa', 'su']:
                            api_params['date'] = date_type
                        else:
                            normalized_date = normalize_date_format(date_type)
                            if normalized_date:
                                api_params['date'] = normalized_date

                if params.get("json_output", True):
                    api_params['json'] = 'y'

                print(f"Making call to station schedule endpoint for station: {station_param}")
                print(f"With parameters: {api_params}")
                return await call_bart_api('sched', 'stnsched', api_params)

            
            elif api_endpoint == "routesched":
                route = params.get("route", "1")
                
                if not (route.isdigit() and 1 <= int(route) <= 12):
                    route_numbers = get_route_from_color(route)
                    
                    if not route_numbers:
                        color = extract_route_color_from_query(query_text)
                        if color:
                            route_numbers = get_route_from_color(color)
                    
                    if route_numbers:
                        route = route_numbers[0]
                        logging.info(f"Mapped color to route number '{route}' for routesched API call")
                    else:
                        logging.warning(f"Invalid route parameter: {route}")
                        return {"error": "Route must be a number between 1 and 12,19,20 or a valid line color (Yellow, Orange, Green, Red, Blue, Grey)"}
                    
                api_params = {'route': route}
                
                if "date" in params:
                    if params["date"].lower() in ['today', 'now', 'wd', 'sa', 'su']:
                        api_params['date'] = params["date"].lower()
                        print(f"Using date code: {params['date'].lower()}")
                    else:
                        api_params['date'] = params["date"]
                        print(f"Using date: {params['date']}")
                else:
                    date_type = extract_date_type_from_query(query_text)
                    if date_type:
                        api_params['date'] = date_type
                        print(f"Using date type extracted from query: {date_type}")
                
                if params.get("show_legend", False):
                    api_params['l'] = '1'
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                
                print(f"Making call to route schedule endpoint for route: {route}")
                return await call_bart_api('sched', 'routesched', api_params)
            
            elif api_endpoint == "scheds":
                # Required: cmd=scheds (no other parameters)
                print("Making call to schedules list endpoint")
                return await call_bart_api('sched', 'scheds', {})
            
            #---------------------------#
            # STATION CATEGORY ENDPOINTS
            #---------------------------#
            elif api_endpoint == "stninfo":
                # Required: cmd=stninfo, orig
                station_param = params.get("station")
                if not station_param:
                    print("Missing station parameter")
                    return None
                    
                station_code = get_station_code(station_param)
                if not isinstance(station_code, str):
                    print(f"Invalid station code: {station_code}")
                    return None
                
                api_params = {'orig': station_code}
                
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                
                print(f"Making call to station info endpoint for station: {station_param}")
                return await call_bart_api('stn', 'stninfo', api_params)
            
            elif api_endpoint == "stns":
                # Required: cmd=stns
                api_params = {}
                
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                    
                print("Making call to stations list endpoint")
                return await call_bart_api('stn', 'stns', api_params)
            
            elif api_endpoint == "stnaccess":
                # Required: cmd=stnaccess, orig
                station_param = params.get("station")
                if not station_param:
                    print("Missing station parameter")
                    return None
                    
                station_code = get_station_code(station_param)
                if not isinstance(station_code, str):
                    print(f"Invalid station code: {station_code}")
                    return None
                
                api_params = {'orig': station_code}
                
                if params.get("show_legend", False):
                    api_params['l'] = '1'
                
                if params.get("json_output", True):
                    api_params['json'] = 'y'
                
                print(f"Making call to station access endpoint for station: {station_param}")
                return await call_bart_api('stn', 'stnaccess', api_params)
        
        if (is_complete_query or ('orig' in params and 'dest' in params)) and category in ["REAL_TIME", "SCHEDULE"]:
            print("Detected query with both origin and destination stations")
            
            orig = params.get("orig") or params.get("station")
            dest = params.get("dest") or params.get("station")
            
            if is_complete_query and origin_station and destination_station:
                orig = origin_station
                dest = destination_station
                print(f"Using stations from websocket state: origin={orig}, destination={dest}")
            
            if not orig or not dest:
                print("Missing origin or destination parameters")
                return None
                
            orig_code = get_station_code(orig)
            dest_code = get_station_code(dest)
            
            if not isinstance(orig_code, str) or not isinstance(dest_code, str):
                print(f"Invalid station codes: origin={orig_code}, dest={dest_code}")
                return None 
                
            use_arrive = False
            arrive_keywords = ["arrive", "arrival", "arriving", "get to", "reach", "be at"]
            for keyword in arrive_keywords:
                if keyword in query_text.lower():
                    use_arrive = True
                    break
            
            api_params = {
                'orig': orig_code,
                'dest': dest_code,
            }
            
            query_lower = query_text.lower()
            
            before_val = 0
            after_val = 3
            
            if "before" in query_lower:
                before_val = 3
                after_val = 0
            elif "after" in query_lower:
                before_val = 0
                after_val = 3
            
            train_count = extract_train_count_from_query(query_text)
            if train_count is not None:
                if "before" in query_lower:
                    before_val = train_count
                    after_val = 0
                elif "after" in query_lower:
                    before_val = 0
                    after_val = train_count
                else:
                    before_val = 0
                    after_val = train_count
            
            api_params['b'] = str(before_val)
            api_params['a'] = str(after_val)
            
            if "time" in params:
                normalized_time = normalize_time_format(params["time"])
                if normalized_time:
                    api_params['time'] = normalized_time
            
            if "date" in params:
                if params["date"].lower() in ['today', 'now', 'wd', 'sa', 'su']:
                    api_params['date'] = params["date"].lower()
                    print(f"Using date code: {params['date'].lower()}")
                else:
                    api_params['date'] = params["date"]
                    print(f"Using date: {params['date']}")
            else:
                date_type = extract_date_type_from_query(query_text)
                if date_type:
                    api_params['date'] = date_type
                    print(f"Using date type extracted from query: {date_type}")
            
            station_names = extract_station_names(query_text)
            
            if len(station_names) == 1 or category == "REAL_TIME":
                print(f"Single station query or REAL_TIME category detected, using etd endpoint for station: {orig}")
                return await call_bart_api('etd', 'etd', {'orig': orig_code})
            else:
                if use_arrive:
                    print(f"Making arrivals schedule call from {orig} to {dest}")
                    temp_b = api_params['b']
                    api_params['b'] = api_params['a']
                    api_params['a'] = temp_b
                    return await call_bart_api('sched', 'arrive', api_params)
                else:
                    print(f"Making departures schedule call from {orig} to {dest}")
                    return await call_bart_api('sched', 'depart', api_params)
        
        #---------------------------#
        # ADVISORY CATEGORY FALLBACKS
        #---------------------------#
        if category == "ADVISORY":
            if "elevator" in query_text.lower() or "escalator" in query_text.lower():
                query_lower = query_text.lower()
                if "elevator" in query_lower and "escalator" not in query_lower:
                    eq_type = "elevator"
                elif "escalator" in query_lower and "elevator" not in query_lower:
                    eq_type = "escalator"
                else:
                    eq_type = "elevator"
                
                station_param = params.get("station", "all")
                station_code = get_station_code(station_param) if station_param and station_param != "all" else None
                
                api_params = {
                    'eq': eq_type
                }
                if station_code:
                    api_params['stn'] = station_code
                
                print(f"Detected {eq_type} status query, using ets endpoint")
                return await call_bart_api('ets', 'status', api_params)
            
            elif "count" in query_text.lower() or "how many" in query_text.lower() or "number of" in query_text.lower():
                print("Detected train count query, using count endpoint")
                return await call_bart_api('bsa', 'count', {})
            
            else:
                station_param = params.get("station", "all")
                station_code = get_station_code(station_param) if station_param and station_param != "all" else "all"
                print(f"Using general advisory endpoint for station: {station_param or 'all'}")
                return await call_bart_api('bsa', 'bsa', {'orig': station_code})
        
        #---------------------------#
        # REAL_TIME CATEGORY FALLBACKS
        #---------------------------#
        elif category == "REAL_TIME":
            station_param = params.get("station", "ALL")
            station_code = get_station_code(station_param) if station_param and station_param.lower() != "all" else "ALL"
            
            api_params = {'orig': station_code}
            if "platform" in params:
                api_params['platform'] = params["platform"]
            if "direction" in params:
                api_params['direction'] = params["direction"]
                
            print(f"Using real-time departures endpoint for station: {station_param}")
            return await call_bart_api('etd', 'etd', api_params)
        
        #---------------------------#
        # ROUTE CATEGORY FALLBACKS
        #---------------------------#
        elif category == "ROUTE":
            if "route" in params:
                route = params['route']
                
                if not (route.isdigit() and 1 <= int(route) <= 12) and route.lower() != 'all':
                    route_numbers = get_route_from_color(route)
                    
                    if not route_numbers:
                        color = extract_route_color_from_query(query_text)
                        if color:
                            route_numbers = get_route_from_color(color)
                    
                    if route_numbers:
                        route = route_numbers[0]
                        logging.info(f"Mapped color to route number '{route}' in route fallback")
                
                print(f"Using route info endpoint for route: {route}")
                return await call_bart_api('route', 'routeinfo', {'route': route})
            else:
                color = extract_route_color_from_query(query_text)
                if color:
                    route_numbers = get_route_from_color(color)
                    if route_numbers:
                        route = route_numbers[0]
                        logging.info(f"Extracted color from query and mapped to route number '{route}'")
                        print(f"Using route info endpoint for route: {route}")
                        return await call_bart_api('route', 'routeinfo', {'route': route})
                
                print("No specific route requested, using routes list endpoint")
                return await call_bart_api('route', 'routes', {})
        
        #---------------------------#
        # SCHEDULE CATEGORY FALLBACKS
        #---------------------------#
        elif category == "SCHEDULE":
            if ("station" in params or "orig" in params) and "schedule" in query_text.lower():
                station_code = get_station_code(params.get("station") or params.get("orig"))
                if isinstance(station_code, str):
                    api_params = {'orig': station_code}
                    
                    if "date" in params:
                        if params["date"].lower() in ['today', 'now', 'wd', 'sa', 'su']:
                            api_params['date'] = params["date"].lower()
                        elif params["date"].lower() in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                            api_params['date'] = 'wd'
                        elif params["date"].lower() == 'saturday':
                            api_params['date'] = 'sa'
                        elif params["date"].lower() == 'sunday':
                            api_params['date'] = 'su'
                        else:
                            normalized_date = normalize_date_format(params["date"])
                            if normalized_date:
                                api_params['date'] = normalized_date

                    if "time" in params:
                        normalized_time = normalize_time_format(params["time"])
                        if normalized_time:
                            api_params['time'] = normalized_time
                            api_params['json'] = 'y'  

                    print(f"Using station schedule endpoint for station: {params['station'] or params['orig']}")
                    return await call_bart_api('sched', 'stnsched', api_params)

            if api_endpoint == "stnsched" and ("station" in params or "orig" in params):
                station_param = params.get("station") or params.get("orig")
                if station_param:
                    station_code = get_station_code(station_param)
                    if isinstance(station_code, str):
                        api_params = {'orig': station_code}
                        
                        if "date" in params:
                            if params["date"].lower() in ['today', 'now', 'wd', 'sa', 'su']:
                                api_params['date'] = params["date"].lower()
                            else:
                                normalized_date = normalize_date_format(params["date"])
                                if normalized_date:
                                    api_params['date'] = normalized_date
                        
                        print(f"Making call to station schedule endpoint for station: {station_param}")
                        return await call_bart_api('sched', 'stnsched', api_params)

        
        #---------------------------#
        # STATION CATEGORY FALLBACKS
        #---------------------------#
        elif category == "STATION":
            if "all" in query_text.lower() and "station" in query_text.lower():
                print("Query is about all stations, using stations list endpoint")
                return await call_bart_api('stn', 'stns', {})
                       
            if "station" in params:
                station_code = get_station_code(params["station"])
                if isinstance(station_code, str):
                    print(f"Using station info endpoint for station: {params['station']}")
                    return await call_bart_api('stn', 'stninfo', {'orig': station_code})
            
            print("No specific station requested, using stations list endpoint")
            return await call_bart_api('stn', 'stns', {})
        
        return None
        
    except Exception as e:
        print(f"\nError in call_api_for_query: {str(e)}")
        return None

# ======================================WEBSOCKET HANDLER======================================
@app.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket):
    await websocket.accept()
    buffer = bytearray()
    
    pre_buffer = collections.deque(maxlen=32000)
    
    last_process_time = 0
    silence_counter = 0
    
    websocket.state.response_sent = False
    websocket.state.last_saved_conversation = None
    websocket.state.skip_current_response = False
    websocket.state.awaiting_disambiguation = False
    websocket.state.awaiting_station_validation = False
    websocket.state.invalid_station = None
    websocket.state.station_suggestions = None
    websocket.state.original_query = None
    websocket.state.ambiguous_station = None
    websocket.state.station_options = None
    websocket.state.input_mode = None  
    websocket.state.skip_tts = False   
    websocket.state.is_health_check = False  
    
    user_id = None
    session_id = None
    user_email = None
    original_query = None  
    
    import re
    
    connection_params = dict(websocket.query_params)
    if "health_check" in connection_params:
        health_check_param = connection_params.get("health_check", "false").lower()
        websocket.state.is_health_check = health_check_param in ("true", "1", "yes")
        logging.info(f"Health check mode enabled: {websocket.state.is_health_check}")
    
    if "skip_tts" in connection_params:
        skip_tts_param = connection_params.get("skip_tts", "false").lower()
        websocket.state.skip_tts = skip_tts_param in ("true", "1", "yes")
        logging.info(f"Skip TTS mode enabled: {websocket.state.skip_tts}")
    
    logging.info("New WebSocket connection established")
    
    try:
        # --------- INITIALIZATION PHASE ---------
        try:
            init_message = await websocket.receive()
        except RuntimeError as re:
            if "disconnect message has been received" in str(re):
                logging.info("Client disconnected during initialization")
                return
            elif "close message has been sent" in str(re):
                logging.info("WebSocket connection closed during initialization")
                return
            else:
                logging.error(f"RuntimeError during WebSocket initialization: {str(re)}")
                return
        except WebSocketDisconnect:
            logging.info("WebSocket disconnected during initialization")
            return
            
        try:
            if 'text' in init_message:
                init_data = json.loads(init_message['text'])
            elif 'bytes' in init_message:
                init_data = json.loads(init_message['bytes'].decode())
            else:
                raise ValueError("Invalid message format")

            user_id = init_data.get('user_id')
            user_email = init_data.get('email')
            session_id = init_data.get('session_id')
            language = init_data.get('language', 'en')
            skip_tts = init_data.get('skip_tts', False)  
            websocket.state.skip_tts = skip_tts
            
            if not user_id:
                raise ValueError("No user_id (name) provided")
            
            if not session_id:
                raise ValueError("No session_id provided")
            
            websocket.state.session_id = session_id
            websocket.state.user_id = user_id
            websocket.state.language = language 
            
            if language not in SUPPORTED_LANGUAGES:
                logging.warning(f"Unsupported language: {language}, defaulting to English")
                websocket.state.language = 'en'
                
            logging.info(f"Language set to: {SUPPORTED_LANGUAGES[websocket.state.language]['name']}")
            
            await create_or_update_user(user_id=user_id, email=user_email)
            logging.info(f"Initialized WebSocket for user: {user_id} with email: {user_email} and session: {session_id}")
            
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"Invalid initialization data: {str(e)}")
            try:
                await websocket.close(code=1003)
            except RuntimeError as close_error:
                if "websocket.close" in str(close_error):
                    logging.info("WebSocket already closed")
                else:
                    logging.error(f"Error closing WebSocket: {str(close_error)}")
            return
        
        # --------- MAIN PROCESSING LOOP ---------
        while True:
            try:
                if websocket.state.is_health_check and websocket.state.response_sent:
                    logging.info("Health check completed, exiting receive loop")
                    break
                
                try:
                    message = await websocket.receive()
                except RuntimeError as re:
                    if "disconnect message has been received" in str(re):
                        logging.info("WebSocket disconnect message received, ending gracefully")
                        break
                    elif "close message has been sent" in str(re):
                        logging.info("WebSocket close message sent, ending gracefully")
                        break
                    else:
                        logging.error(f"RuntimeError in websocket: {str(re)}")
                        break
                except WebSocketDisconnect:
                    logging.info("WebSocket disconnected")
                    break
                
                if 'text' in message:
                    websocket.state.input_mode = "text"
                    text = message['text'].strip()
                    
                    if not text:
                        continue
                    
                    logging.info(f"Received text: '{text}'")
                    
                    websocket.state.response_sent = False
                    websocket.state.last_saved_conversation = None
                    websocket.state.skip_current_response = False
                    
                    original_language = websocket.state.language
                    original_text = text
                    
                    if original_language != 'en':
                        logging.info(f"Translating query from {original_language} to English for processing")
                        try:
                            english_text = await translate_text(original_text, source_lang=original_language, target_lang='en')
                            logging.info(f"Translated query: '{english_text}'")
                            text = english_text
                            websocket.state.original_query_language = original_language
                            websocket.state.original_query_text = original_text
                        except Exception as e:
                            logging.error(f"Error translating query: {str(e)}")
                    
                    print("\n========== PROCESSING QUERY ==========")
                    print(f"Text input: '{text.strip()}'")
                    print(f"Awaiting disambiguation: {getattr(websocket.state, 'awaiting_disambiguation', False)}")
                    print(f"Awaiting station validation: {getattr(websocket.state, 'awaiting_station_validation', False)}")
                    print(f"Original query: '{getattr(websocket.state, 'original_query', None)}'")
                    print(f"Ambiguous station: '{getattr(websocket.state, 'ambiguous_station', None)}'")
                    print(f"Invalid station: '{getattr(websocket.state, 'invalid_station', None)}'")
                    print(f"Station options: {getattr(websocket.state, 'station_options', None)}")
                    print("======================================\n")
                    
                elif 'bytes' in message:
                    websocket.state.input_mode = "audio"
                    chunk = message['bytes']
                    buffer.extend(chunk)
                    current_time = asyncio.get_event_loop().time()
                    
                    if len(buffer) % 8000 == 0 or len(buffer) >= CHUNK_SIZE:
                        logging.info(f"Current buffer size: {len(buffer)} bytes")

                    should_process = (
                        len(buffer) >= CHUNK_SIZE or 
                        (len(buffer) > 0 and current_time - last_process_time > 0.05) or
                        (len(chunk) < 1000 and silence_counter > 1)
                    )
                    
                    if should_process and len(buffer) > 0:
                        try:
                            logging.info(f"Processing audio buffer of {len(buffer)} bytes")
                            language_code = SUPPORTED_LANGUAGES[websocket.state.language]['transcribe_code']
                            text = await transcribe_stream(buffer, language_code, websocket)
                            buffer.clear() 
                            pre_buffer.clear() 
                            last_process_time = current_time
                            silence_counter = 0
                            
                            websocket.state.response_sent = False
                            websocket.state.last_saved_conversation = None
                            websocket.state.skip_current_response = False
                            
                            if not getattr(websocket.state, 'awaiting_disambiguation', False) and not getattr(websocket.state, 'awaiting_location', False):
                                websocket.state.complete_query = False
                            
                            if text and len(text.strip()) > 0:
                                logging.info(f"Transcribed text: '{text.strip()}'")
                                original_language = websocket.state.language

                                original_text = text.strip()
                                
                                if original_language != 'en':
                                    logging.info(f"Translating query from {original_language} to English for processing")
                                    try:
                                        english_text = await translate_text(original_text, source_lang=original_language, target_lang='en')
                                        logging.info(f"Translated query: '{english_text}'")
                                        text = english_text
                                        websocket.state.original_query_language = original_language
                                        websocket.state.original_query_text = original_text
                                    except Exception as e:
                                        logging.error(f"Error translating query: {str(e)}")
                                
                                # --------- QUERY PROCESSING ---------
                                print("\n========== PROCESSING QUERY ==========")
                                print(f"Transcribed text: '{text.strip()}'")
                                print(f"Awaiting disambiguation: {getattr(websocket.state, 'awaiting_disambiguation', False)}")
                                print(f"Awaiting station validation: {getattr(websocket.state, 'awaiting_station_validation', False)}")
                                print(f"Original query: '{getattr(websocket.state, 'original_query', None)}'")
                                print(f"Ambiguous station: '{getattr(websocket.state, 'ambiguous_station', None)}'")
                                print(f"Invalid station: '{getattr(websocket.state, 'invalid_station', None)}'")
                                print(f"Station options: {getattr(websocket.state, 'station_options', None)}")
                                print("======================================\n")
                            else:
                                logging.info("No transcription text received, continuing to next message")
                                continue
                        except Exception as e:
                            logging.error(f"Error processing audio buffer: {str(e)}")
                            continue
                    else:
                        continue
                else:
                    continue
                                
                if getattr(websocket.state, 'awaiting_station_validation', False):
                    logging.info(f"Processing response for invalid station: {websocket.state.invalid_station}")
                    
                    invalid_station = getattr(websocket.state, 'invalid_station', None)
                    station_suggestions = getattr(websocket.state, 'station_suggestions', [])
                    original_query = getattr(websocket.state, 'original_query', None)
                                    
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
                                        
                        websocket.state.awaiting_station_validation = False
                        websocket.state.invalid_station = None
                        websocket.state.station_suggestions = None
                                        
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
                            
                            user_language = getattr(websocket.state, 'language', 'en')
                            
                            translated_query = original_query
                            if user_language != 'en':
                                translated_query = await translate_text(original_query, source_lang='en', target_lang=user_language)
                            
                            replacement_info = {
                                'type': 'conversation',
                                'conversation_id': str(uuid.uuid4()),
                                'timestamp': datetime.now().isoformat(),
                                'query': translated_query,
                                'response': replacement_messages.get(user_language, replacement_messages['en']),
                                'session_id': getattr(websocket.state, 'session_id', None),
                                'is_station_replacement': True
                            }
                            await websocket.send_text(json.dumps(replacement_info, ensure_ascii=False))
                            
                            websocket.state.skip_current_response = True
                            
                            websocket.state.replaced_query = {
                                'original': original_query,
                                'invalid_station': invalid_station,
                                'replacement': valid_station_name,
                                'final': final_query
                            }
                                            
                            await process_query_and_respond_with_text(final_query, websocket)
                                            
                            continue
                        else:
                            additional_message = "That still doesn't match a valid BART station. Please try again with a station from the list."
                            await websocket.send_text(additional_message)
                            audio_bytes = synthesize_speech(additional_message)
                            if audio_bytes:
                                await websocket.send_bytes(audio_bytes)
                                            
                            websocket.state.awaiting_station_validation = True
                            websocket.state.invalid_station = invalid_station
                            websocket.state.station_suggestions = station_suggestions
                            websocket.state.original_query = original_query
                    else:
                        logging.error("Missing required state for station validation")
                        websocket.state.awaiting_station_validation = False
                                
                elif getattr(websocket.state, 'awaiting_disambiguation', False):
                    selected_station = await process_station_disambiguation(
                        text.strip(), 
                        websocket.state.station_options,
                        websocket
                    )
                                    
                    if selected_station:
                        original_query = websocket.state.original_query
                        ambiguous_station = websocket.state.ambiguous_station
                                        
                        pattern = re.compile(re.escape(ambiguous_station), re.IGNORECASE)
                        final_query = pattern.sub(selected_station, original_query)
                                        
                        print("\n========== STATION DISAMBIGUATION COMPLETE ==========")
                        print(f"Original query: '{original_query}'")
                        print(f"Ambiguous station: '{ambiguous_station}'")
                        print(f"Selected station: '{selected_station}'")
                        print(f"Final query: '{final_query}'")
                        print("====================================================\n")
                                        
                        logging.info(f"Disambiguated query: '{final_query}' (original: '{original_query}')")
                                    
                        websocket.state.awaiting_disambiguation = False
                        websocket.state.original_query = None
                        websocket.state.station_options = None
                        websocket.state.ambiguous_station = None
                                        
                        combined_response = await process_query_with_disambiguation(final_query, websocket)
                                         
                        if combined_response:
                            final_response = combined_response
                            if "Final Combined Response:" in combined_response:
                                final_response = combined_response.split("Final Combined Response:", 1)[1].strip()
                                if "--------" in final_response:
                                    final_response = final_response.split("--------", 1)[1].strip()
                                            
                            conversation_data = {
                                'type': 'conversation',
                                'query': final_query,  
                                'response': final_response,
                                'timestamp': datetime.utcnow().isoformat(),
                                'session_id': session_id,
                                'is_final_disambiguation': True  
                            }

                            # --------- SAVE FINAL CONVERSATION ---------
                            websocket.state.last_saved_conversation = final_query
                            
                            final_response = extract_final_response(combined_response)
                            await save_and_send_response(
                                websocket, user_id, final_query, combined_response, final_response, session_id
                            )
                            logging.info(f"Processed disambiguated query: {final_query}")
                    else:
                        print("\n========== DISAMBIGUATION FAILED ==========")
                        print(f"Could not determine selection from: '{text}'")
                        print("Falling back to normal query processing")
                        print("==========================================\n")
                                        
                        websocket.state.awaiting_disambiguation = False
                        websocket.state.original_query = None
                        websocket.state.station_options = None
                        websocket.state.ambiguous_station = None
                                        
                        combined_response = await process_query_with_disambiguation(text, websocket)
                                        
                        if combined_response:
                            final_response = extract_final_response(combined_response)
                            await save_and_send_response(
                                websocket, user_id, text.strip(), combined_response, final_response, session_id
                            )
                else:
                    if getattr(websocket.state, 'skip_current_response', False):
                        websocket.state.skip_current_response = False
                        print("Skipping processing of current response as it was part of station validation")
                        continue
                                    
                    combined_response = await process_query_with_disambiguation(text, websocket)
                                    
                    if combined_response and not getattr(websocket.state, 'awaiting_disambiguation', False):
                        final_response = extract_final_response(combined_response)
                        await save_and_send_response(
                            websocket, user_id, text.strip(), combined_response, final_response, session_id
                        )
                                
            except WebSocketDisconnect:
                logging.info("WebSocket disconnected during response, waiting for new connection")
                break
            except RuntimeError as re:
                if "close message has been sent" in str(re):
                    logging.info("WebSocket already closed, waiting for new connection")
                    break
                else:
                    raise
            except Exception as e:
                logging.exception(f"Error while processing query: {str(e)}")
                try:
                    fallback_audio = synthesize_speech("Oops, something went wrong. Try asking again?")
                    await websocket.send_bytes(fallback_audio)
                    await websocket.send_text("Error processing query. Please try again.")
                except (WebSocketDisconnect, RuntimeError):
                    logging.info("WebSocket disconnected during error handling, waiting for new connection")
                    break
                except Exception as err:
                    logging.exception(f"Error sending fallback response: {str(err)}")
                    try:
                        await websocket.close(code=1011)
                    except RuntimeError as close_err:
                        if "websocket.close" in str(close_err):
                            logging.info("WebSocket already closed")
                        else:
                            logging.error(f"Error closing WebSocket: {str(close_err)}")
                    break
                    
    except WebSocketDisconnect:
        logging.info("WebSocket disconnected during processing")
    except RuntimeError as re:
        if "disconnect message has been received" in str(re):
            logging.info("WebSocket disconnect message received in main handler")
        elif "close message has been sent" in str(re):
            logging.info("WebSocket close message sent in main handler")
        else:
            logging.exception(f"RuntimeError in websocket handler: {str(re)}")
    except Exception as e:
        logging.exception(f"Unexpected error in websocket handler: {str(e)}")
        try:
            if not getattr(websocket, "client_state", None) or not websocket.client_state.DISCONNECTED:
                fallback_audio = synthesize_speech("Sorry, I couldn't process that request. Try asking again.")
                await websocket.send_bytes(fallback_audio)
                try:
                    await websocket.close(code=1011) 
                except RuntimeError as close_err:
                    if "websocket.close" in str(close_err):
                        logging.info("WebSocket already closed")
                    else:
                        logging.error(f"Error closing WebSocket: {str(close_err)}")
        except (WebSocketDisconnect, RuntimeError) as err:
            logging.info(f"WebSocket error during fallback response: {str(err)}")
        except Exception as err:
            logging.exception(f"Error sending fallback response: {str(err)}")
    finally:
        logging.info(f"WebSocket handler completed for user {user_id}")

# ======================================QUERY PROCESSING FUNCTION======================================
async def process_query_and_respond_with_text(query_text: str, websocket: WebSocket):
    """Process user query and return combined text response"""
    try:
        query_text = query_text.replace("Street", "St.").replace("street", "St.")
        # --------- PERFORM INITIAL QUERY TYPE CLASSIFICATION FIRST ---------
        classification_response = await non_streaming_claude_response({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": [{"type": "text", "text": QUERY_TYPE_CLASSIFICATION_PROMPT.format(query_text=query_text)}]}]
        })
        
        print("\n========== QUERY TYPE CLASSIFICATION ==========")
        print(f"Classification: {classification_response}")
        print("============================================\n")
        
        if not classification_response:
            print("Error: Failed to get classification response from model")
            fallback_audio = synthesize_speech("I'm having trouble understanding that. Could you try again?")
            if fallback_audio:
                await websocket.send_bytes(fallback_audio)
            return "Error: Failed to classify query"
        
        classification_text = classification_response.strip().lower()
        valid_types = ["greeting", "api", "kb", "stop_command", "off_topic", "off-topic"]
        if classification_text in valid_types:
            if classification_text == "off-topic":
                query_type = "off_topic"
            else:
                query_type = classification_text
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
            else:
                if "schedule" in classification_text:
                    query_type = "api"
                    logging.warning(f"Received 'schedule' classification, converting to 'api'")
                else:
                    query_type = "kb"
                    logging.warning(f"Unclear classification response: '{classification_text}', defaulting to 'kb'")

        # --------- EXTRACT POTENTIAL STATIONS FOR EARLY VALIDATION ---------
        if query_type != "kb":
            station_candidates = extract_station_names(query_text)
            if station_candidates:
                print(f"\n========== EARLY STATION VALIDATION ==========")
                print(f"Extracted station candidates: {station_candidates}")
                
                validation_result = validate_stations(station_candidates)
                if not validation_result["valid"]:
                    print(f"Found invalid stations during early validation: {validation_result['invalid_stations']}")
                    
                    try:
                        updated_query = await process_station_validation(
                            query_text, 
                            validation_result["invalid_stations"],
                            validation_result["suggestions"],
                            websocket
                        )
                        
                        if updated_query:
                            print(f"Updated query after early station validation: {updated_query}")
                            query_text = updated_query
                        else:
                            print("Early station validation process already handled the invalid station")
                            return None
                    except Exception as e:
                        print(f"Error during early station validation: {str(e)}")
        else:
            print(f"\n========== SKIPPING STATION VALIDATION FOR KB QUERY ==========")
            print(f"Query classified as KB, skipping station validation.")
            
        # --------- THINKING MESSAGE DEFINITION ---------
        language = getattr(websocket.state, 'language', 'en')
        selected_thinking_messages = thinking_messages.get(language, thinking_messages["en"])

        print("\n========== USER QUERY ==========")
        print(f"User Query: {query_text}")
        print("===============================\n")
        
        session_id = getattr(websocket.state, 'session_id', None)
        user_id = getattr(websocket.state, 'user_id', None)
        websocket_language = getattr(websocket.state, 'language', 'en')
        original_query_language = getattr(websocket.state, 'original_query_language', websocket_language)
        original_query_text = getattr(websocket.state, 'original_query_text', query_text)
        
        language = websocket_language
        
        if not session_id or not user_id:
            logging.error("Missing session_id or user_id in websocket state")
            return "Error: Session information not found"
        
       
        
        contains_station_name = False
        mentioned_station = None
        query_lower = query_text.lower()
        
        if query_type != "kb":
            import re
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
        
        print(f"Query classified as: {query_type} (from: '{classification_text}')")
        
        # --------- SEND THINKING MESSAGE AFTER CLASSIFICATION ---------
        if query_type != 'greeting' and query_type != 'stop_command':
            thinking_msg = random.choice(selected_thinking_messages)
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
            
            print(f"\n========== SENDING THINKING MESSAGE ==========")
            print(f"Time: {current_time}")
            print(f"Message: '{thinking_msg}'")
            print(f"Query type: '{query_type}' (non-greeting/non-stop)")
            print("===============================================\n")
            
            await websocket.send_json({
                "type": "thinking_message",
                "message": thinking_msg,
                "timestamp": current_time
            })
            
            thinking_audio = synthesize_speech(thinking_msg, language=language)
            if thinking_audio:
                print(f"Thinking message audio size: {len(thinking_audio)} bytes")
                print(f"Sending thinking audio at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
                await websocket.send_bytes(thinking_audio)
                print(f"Thinking message audio sent at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
                
                print(f"Waiting to ensure thinking message is processed...")
                await asyncio.sleep(0.5)  
                print(f"Continuing with query processing at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
        else:
            print(f"\n========== SKIPPING THINKING MESSAGE ==========")
            print(f"Query type: '{query_type}' (greeting or stop command)")
            print("=================================================\n")
        
        # --------- HANDLE OFF-TOPIC QUERY ---------
        if query_type == 'off_topic':
            redirect_messages = {
                "en": "I'm a BART assistant and can only answer questions about BART transit. How can I help you with BART information today?",
                "es": "Soy un asistente de BART y solo puedo responder preguntas sobre el tránsito de BART. ¿Cómo puedo ayudarte con información de BART hoy?",
                "zh": "我是BART助手，只能回答有关BART交通的问题。今天我能为您提供什么BART信息？"
            }           
            redirect_message = redirect_messages.get(language, redirect_messages["en"])
            audio_data = synthesize_speech(redirect_message, language=language)
            if audio_data:
                await websocket.send_bytes(audio_data)
            
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
        
        # --------- HANDLE STOP COMMAND ---------
        if query_type == 'stop_command':
            await websocket.send_json({"type": "stop_listening"})
            return None
        
        # --------- HANDLE GREETING ---------
        if query_type == 'greeting':
            greeting_response = await non_streaming_claude_response({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "temperature": 0.4,
                "messages": [{"role": "user", "content": [{"type": "text", "text": f"""
                    Respond to this greeting in a friendly, professional way. 
                    Do not include any prefixes, emojis, or meta-text.
                    Follow these rules:
                    - Respond in a friendly, professional way as like a human-human conversation.
                    - DO NOT use emojis or emoticons like :) or :-)
                    - For initial greetings (hi, hello): Respond warmly and ask about BART needs
                    - For thank you messages: Acknowledge politely and offer more help
                    - Keep the response brief and professional
                    - No exclamation marks
                    - Always relate back to BART assistance

                    
                    Examples of good responses:
                    For greetings:
                        "Hello. How can I help with BART transit today?"
                        "Good morning. What BART information do you need?"
                    
                    For thank you messages:
                        "You're welcome. What other BART information can I help you with?"
                        "You're welcome. Let me know if you need anything else about BART."
                    
                    User greeting: {query_text}
                """}]}]
            })
            
            print("\n========== GREETING RESPONSE 1 ==========")
            print(f"Response1: {greeting_response}")
            print("====================================\n")
            
            if not greeting_response:
                print("Error: Failed to get greeting response from model")
                fallback_audio = synthesize_speech("Hello! How can I help you with BART today?")
                if fallback_audio:
                    await websocket.send_bytes(fallback_audio)
                return "Error: Failed to generate greeting response"
            
            cleaned_response = greeting_response.replace("Here's a friendly greeting response:", "").replace("Here is a friendly greeting response:", "").strip()
            if cleaned_response.startswith('"') and cleaned_response.endswith('"'):
                cleaned_response = cleaned_response[1:-1].strip()
            
            audio_data = synthesize_speech(cleaned_response, language=language)
            if audio_data:
                await websocket.send_bytes(audio_data)
            
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
            # --------- INTENT CLASSIFICATION ---------
            # Step 1: Classify the intent and extract parameters for API calls
            intent_response = await non_streaming_claude_response({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1000,
                    "temperature": 0.1,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": INTENT_CLASSIFICATION_PROMPT.format(query_text=query_text)}]}]
            })
            
            print("\n========== API INTENT CLASSIFICATION 1==========")
            print(f"Intent Response: {intent_response}")
            print("=========================================\n")
            
            try:
                import re
                json_pattern = r'(\{[\s\S]*\})'
                json_matches = re.search(json_pattern, intent_response)
                
                if json_matches:
                    json_str = json_matches.group(1)
                    intent_data = json.loads(json_str)
                else:
                    intent_data = json.loads(intent_response)
                    
                is_api_related = intent_data.get("is_api_related", False)  
                bart_data = None
                api_response_text = "No API data available"
                
                category = intent_data.get("category", "UNKNOWN")
                params = intent_data.get("parameters", {})
                api_endpoint = intent_data.get("api_endpoint", None)
                
                print("\n========== API PARAMETERS ANALYSIS ==========")
                print(f"Category: {category}")
                print(f"API Related: {is_api_related}")
                print(f"API Endpoint: {api_endpoint}")
                print(f"Parameters: {json.dumps(params, indent=2, ensure_ascii=False)}")
                print("============================================\n")
                
                station_names = extract_station_names(query_text)
                query_lower = query_text.lower()
                departure_keywords = ["depart", "departing", "departure", "leaving", "leave", "next train"]
                arrival_keywords = ["arrive", "arriving", "arrival", "coming", "incoming", "next train"]
                
                has_departure_keyword = any(keyword in query_lower for keyword in departure_keywords)
                has_arrival_keyword = any(keyword in query_lower for keyword in arrival_keywords)
                
                if len(station_names) == 1 and (has_departure_keyword or has_arrival_keyword):
                    if category != "REAL_TIME" or api_endpoint != "etd":
                        print(f"\n========== OVERRIDING CLASSIFICATION ==========")
                        print(f"Found single station ({station_names[0]}) with departure/arrival keywords")
                        print(f"Overriding category from {category} to REAL_TIME")
                        print(f"Overriding endpoint from {api_endpoint} to etd")
                        print("================================================\n")
                        
                        category = "REAL_TIME"
                        api_endpoint = "etd"
                        
                        if "orig" in params:
                            params["station"] = params["orig"]
                            if "station" in params and params["station"] == params["orig"]:
                                del params["orig"]
                        elif "dest" in params:
                            params["station"] = params["dest"]
                            if "station" in params and params["station"] == params["dest"]:
                                del params["dest"]
                
                if query_type != "kb":
                    for key, value in list(params.items()):
                        if key in ['orig', 'dest', 'station'] and value:
                            normalized_value = normalize_station_name(str(value))
                            if normalized_value != value:
                                print(f"Normalized station parameter {key}: '{value}' → '{normalized_value}'")
                                params[key] = normalized_value
                    
                    station_params = []
                    for key, value in params.items():
                        if key in ['orig', 'dest', 'station'] and value:
                            station_params.append(value)
                    
                    if station_params:
                        print(f"\n========== VALIDATING PARAMETER STATIONS ==========")
                        print(f"Station parameters to validate: {station_params}")
                        validation_result = validate_stations(station_params)
                        
                        if not validation_result["valid"]:
                            print(f"Found invalid stations in parameters: {validation_result['invalid_stations']}")
                            
                            try:
                                updated_query = await process_station_validation(
                                    query_text,
                                    validation_result["invalid_stations"],
                                    validation_result["suggestions"],
                                    websocket
                                )
                                
                                if updated_query:
                                    print(f"Updated query after station validation: {updated_query}")
                                    return await process_query_and_respond_with_text(updated_query, websocket)
                                else:
                                    print("Station validation process already handled the invalid station")
                                    return None
                            except Exception as e:
                                print(f"Error during parameter station validation: {str(e)}")
                else:
                    print(f"\n========== SKIPPING PARAMETER STATION VALIDATION FOR KB QUERY ==========")
                    print(f"Query classified as KB, skipping parameter station validation.")
                
                if query_type == "kb":
                    is_api_related = False
                    intent_type = "KB"
                    print(f"Query classified as KB about BART organization/general information, not treating as API related")
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
                    
                    if "to" in query_text.lower() and "from" not in query_text.lower() and "station" in params and category == "REAL_TIME":
                        destination_station = params.get("station")
                        print(f"\nDetected destination-only query for station: {destination_station}")
                        
                        if websocket and not getattr(websocket.state, 'awaiting_location', False):
                            print(f"\nSetting websocket state for location request. Destination: {destination_station}")
                            websocket.state.awaiting_location = True
                            websocket.state.destination_station = destination_station
                            
                            language = getattr(websocket.state, 'language', 'en')
                            location_request = f"To find trains to {destination_station}, I need to know which BART station you're currently at. Please tell me your current station."
                            
                            audio_data = synthesize_speech(location_request, language=language)
                            if audio_data:
                                await websocket.send_bytes(audio_data)
                            await websocket.send_text(location_request)
                            
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
                            await websocket.send_text(json.dumps(conversation_data, ensure_ascii=False))
                            
                            print("\nReturning early from destination-only query to wait for location input")
                            return None
                    
                    bart_data = await call_api_for_query(category, params, query_text, api_endpoint, websocket)
                    
                    if bart_data is None and getattr(websocket.state, 'awaiting_location', False):
                        print("\nAPI call returned None and awaiting_location is True. Stopping processing.")
                        return None
                    
                    print("\nBART API Response:")
                    print(json.dumps(bart_data, indent=2, ensure_ascii=False) if bart_data else "No API data available")
                    print("================================\n")
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
                        
                        station_candidates = []
                        if "parameters" in intent_data:
                            for key, value in intent_data["parameters"].items():
                                if key in ["orig", "dest", "station"] and value:
                                    station_candidates.append(value)
                        
                        if station_candidates:
                            validation_result = validate_stations(station_candidates)
                            if not validation_result["valid"]:
                                try:
                                    updated_query = await process_station_validation(
                                        query_text,
                                        validation_result["invalid_stations"],
                                        validation_result["suggestions"],
                                        websocket
                                    )
                                    
                                    if updated_query:
                                        print(f"Updated query after validation: {updated_query}")
                                        return await process_query_and_respond_with_text(updated_query, websocket)
                                except Exception as e:
                                    print(f"Error handling invalid station: {str(e)}")
                elif query_type == "kb" and bart_data and "error" in bart_data and "Invalid station" in bart_data["error"]:
                    print(f"\n========== SKIPPING INVALID STATION HANDLING FOR KB QUERY ==========")
                    print(f"KB query with potential invalid station reference, but we're prioritizing KB intent")
                
                # Step 2: Always get Knowledge Base data
                kb_context = retrieve_knowledge(query_text)
                kb_response_text = kb_context if kb_context else "No knowledge base information available"
                
                print("\nKnowledge Base Response:")
                print(kb_response_text)
                print("======================================\n")
                
                # Step 3: Generate combined response with proper intent prioritization
                print("\n========== GENERATING FINAL RESPONSE ==========")
                
                if query_type == "kb":
                    intent_type = "KB"
                    print("Query classified as KB question, prioritizing knowledge base")
                elif "category" in intent_data:
                    category = intent_data["category"]
                    is_api_related = intent_data.get("is_api_related", False)
                    
                    if category in ["ADVISORY", "REAL_TIME"] or (category in ["ROUTE", "SCHEDULE", "STATION"] and is_api_related):
                        intent_type = "API"
                    else:
                        intent_type = "KB"
                else:
                    intent_type = "MIXED"
                
                logging.info(f"Query intent determined as: {intent_type}")
                
                if intent_type == "KB" and (not kb_context or kb_context.strip() == ""):
                    logging.info("KB intent but no KB data available, falling back to mixed intent")
                    intent_type = "MIXED"
                
                final_response = await generate_combined_response(
                    query_text, 
                    kb_context, 
                    bart_data,
                    user_id=user_id,
                    session_id=session_id,
                    intent_type=intent_type,
                    language=language
                )
                
                print("\nFinal Response:")
                print(final_response)
                print("==========================================\n")
                
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
                
                # Step 4: Synthesize and stream audio for the final response only
                print("\n========== STREAMING AUDIO RESPONSE ==========")
                
                if original_query_language != 'en':
                    logging.info(f"Translating response back to {original_query_language}")
                    try:
                        translated_response = await translate_text(final_response, source_lang='en', target_lang=original_query_language)
                        logging.info(f"Translated response: {truncate_json_for_logging(translated_response, max_length=150)}")
                        
                        final_response = translated_response
                        combined_text = combined_text.replace("Final Combined Response:\n--------\n", f"Final Combined Response:\n--------\n{translated_response}")
                    except Exception as e:
                        logging.error(f"Error translating response: {str(e)}")
                
                is_station_replacement = getattr(websocket.state, 'replaced_query', None) is not None
                
                phrases = chunk_text_by_words(final_response, language=language)
                for phrase in phrases:
                    if phrase.strip():
                        try:
                            audio_data = synthesize_speech(phrase, language=language)
                            if audio_data:
                                await websocket.send_bytes(audio_data)
                                print(f"Streamed audio for phrase: {phrase}")
                        except (WebSocketDisconnect, RuntimeError) as ws_error:
                            print(f"WebSocket disconnected during audio streaming: {str(ws_error)}")
                            raise
                print("=========================================\n")
                
                await save_and_send_response(websocket, user_id, query_text, combined_text, final_response, session_id)
                
                return combined_text
                
            except json.JSONDecodeError as e:
                print(f"\nError: Failed to parse intent classification response: {e}")
                print("Falling back to knowledge base only\n")
                
                kb_context = retrieve_knowledge(query_text)
                print("\n========== KNOWLEDGE BASE FALLBACK ==========")
                print(f"Query: {query_text}")
                print("\nKnowledge Base Response:")
                print(kb_context if kb_context else "No knowledge base information available")
                print("=========================================\n")
                
                final_response = await generate_combined_response(
                    query_text, 
                    kb_context, 
                    None,
                    intent_type="KB",
                    language=language
                )
                print("\n========== FINAL FALLBACK RESPONSE ==========")
                print(final_response)
                print("=========================================\n")
                
                combined_text = f"""

Final Combined Response:
--------
{final_response}
"""
                
                phrases = chunk_text_by_words(final_response, language=language)
                for phrase in phrases:
                    if phrase.strip():
                        try:
                            audio_data = synthesize_speech(phrase, language=language)
                            if audio_data:
                                await websocket.send_bytes(audio_data)
                                print(f"Streamed audio for phrase: {phrase}")
                        except (WebSocketDisconnect, RuntimeError) as ws_error:
                            print(f"WebSocket disconnected during audio streaming: {str(ws_error)}")
                            raise
                        
                return combined_text
                
    except (WebSocketDisconnect, RuntimeError) as ws_error:
        print(f"\nError: WebSocket connection error in query processing: {str(ws_error)}")
        raise
    except Exception as e:
        print(f"\nError: Error in query processing pipeline: {str(e)}")
        try:
            fallback_audio = synthesize_speech("Sorry, I couldn't process that request. Please try again.")
            await websocket.send_bytes(fallback_audio)
        except (WebSocketDisconnect, RuntimeError):
            print("WebSocket disconnected during fallback response")
            raise
        return "Error processing query. Please try again."

# ======================================AUDIO STREAMING CLASSES======================================
class PCMStream(AudioStream):
    def __init__(self, audio_bytes: bytes):
        self.audio_bytes = audio_bytes
        self.chunk_size = 1024

    async def __aiter__(self):
        for i in range(0, len(self.audio_bytes), self.chunk_size):
            yield self.audio_bytes[i:i + self.chunk_size]

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
async def transcribe_stream(audio_bytes: bytes, language_code: str = "en-US", websocket: WebSocket = None) -> str:
    try:
        print("\n========== TRANSCRIPTION START ==========")
        print(f"Processing {len(audio_bytes)} bytes of audio in language: {language_code}")
        print("========================================\n")
        
        client = TranscribeStreamingClient(region=REGION)
        
        logging.info(f"Starting transcription stream for {len(audio_bytes)} bytes of audio in {language_code}")
        
        stream = await client.start_stream_transcription(
            language_code=language_code,
            media_sample_rate_hz=16000,
            media_encoding="pcm",
            enable_partial_results_stabilization=True,
            partial_results_stability="high",
            show_speaker_label=False,
            enable_channel_identification=False
        )

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
                    
                    language = getattr(websocket.state, 'language', 'en')
                    empty_audio_messages = {
                        'en': "I didn't hear anything. Please try speaking again.",
                        'es': "No escuché nada. Por favor, intenta hablar de nuevo.",
                        'zh': "我没有听到任何内容。请再次尝试说话。"
                    }
                    
                    message = empty_audio_messages.get(language, empty_audio_messages['en'])
                    audio_data = synthesize_speech(message, language=language)
                    
                    if audio_data:
                        await websocket.send_bytes(audio_data)
                        logging.info(f"Sent audio notification for empty audio in {language}")
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
                logging.error(f"Error sending error notification to frontend: {str(ws_error)}")
                
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

def retrieve_knowledge(query: str) -> str:
    print("\n========== KNOWLEDGE BASE RETRIEVAL ==========")
    print(f"Query: {query}")
    print("============================================\n")
    
    kb_instructions = KB_INSTRUCTIONS
    
    enhanced_query = f"""
    User Query: {query}

    Instructions:
    {kb_instructions.strip()}
    """
    
    try:
        response = kb_client.retrieve_and_generate(
            input={"text": enhanced_query},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": KNOWLEDGE_BASE_ID,
                    "modelArn": MODEL_ARN,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": 5,
                            "overrideSearchType": "HYBRID"
                        }
                    }
                }
            }
        )
    except Exception as e:
        logging.error(f"Error calling knowledge base: {str(e)}")
        return f"I'm sorry, I encountered an error while retrieving information from the knowledge base. Error: {str(e)}"
    
    try:
        generated_text = response.get("output", {}).get("text", "No relevant documents found.")
        
        citations = []
        if "citations" in response:
            try:
                for citation in response.get("citations", []):
                    if isinstance(citation.get("retrievedReferences"), dict):
                        ref = citation.get("retrievedReferences", {})
                        source_url = None
                        if isinstance(ref.get("metadata"), dict):
                            source_url = ref.get("metadata", {}).get("source")
                        
                        if not source_url and isinstance(ref.get("content"), dict):
                            source_url = ref.get("content", {}).get("source")
                        
                        content = ""
                        if isinstance(ref.get("content"), dict):
                            content = ref.get("content", {}).get("text", "")
                        elif isinstance(ref.get("content"), str):
                            content = ref.get("content", "")
                        
                        if not source_url and content:
                            url_match = re.search(r'https?://[^\s]+', content)
                            if url_match:
                                source_url = url_match.group(0)
                        
                        if not source_url:
                            source_url = "No source found"
                            
                        if source_url and content:
                            citations.append({"source": source_url, "content": content})
                    elif isinstance(citation.get("retrievedReferences"), list):
                        for ref in citation.get("retrievedReferences", []):
                            if isinstance(ref, dict):
                                source_url = None
                                if isinstance(ref.get("metadata"), dict):
                                    source_url = ref.get("metadata", {}).get("source")
                                
                                if not source_url and isinstance(ref.get("content"), dict):
                                    source_url = ref.get("content", {}).get("source")
                                
                                content = ""
                                if isinstance(ref.get("content"), dict):
                                    content = ref.get("content", {}).get("text", "")
                                elif isinstance(ref.get("content"), str):
                                    content = ref.get("content", "")
                                
                                if not source_url and content:
                                    url_match = re.search(r'https?://[^\s]+', content)
                                    if url_match:
                                        source_url = url_match.group(0)
                                
                                if not source_url:
                                    source_url = "No source found"
                                    
                                if source_url and content:
                                    citations.append({"source": source_url, "content": content})
            except Exception as e:
                logging.error(f"Error processing citations: {str(e)}")
                logging.error(f"Citation structure: {truncate_json_for_logging(response.get('citations', []))}")
        
        retrieved_docs = []
        if "retrievedDocuments" in response:
            try:
                for doc in response.get("retrievedDocuments", []):
                    if isinstance(doc, dict):
                        source_url = None
                        if isinstance(doc.get("metadata"), dict):
                            source_url = doc.get("metadata", {}).get("source")
                        
                        content_data = doc.get("content", {})
                        content = ""
                        
                        if isinstance(content_data, dict):
                            if "text" in content_data:
                                content = content_data.get("text", "")
                            elif "source" in content_data:
                                source_url = content_data.get("source")
                                content = str(content_data)
                        elif isinstance(content_data, str):
                            content = content_data
                        
                        if not source_url and content:
                            url_match = re.search(r'https?://[^\s]+', content)
                            if url_match:
                                source_url = url_match.group(0)
                        
                        if not source_url:
                            source_url = "No source found"
                            
                        if source_url and content:
                            retrieved_docs.append({"source": source_url, "content": content})
                    
                    if len(retrieved_docs) == 0:
                        logging.info(f"First retrieved document structure: {truncate_json_for_logging(doc)}")
            except Exception as e:
                logging.error(f"Error processing retrieved documents: {str(e)}")
                logging.error(f"Document structure: {truncate_json_for_logging(response.get('retrievedDocuments', []))}")
        
        formatted_docs = ""
        
        if citations:
            for doc in citations:
                content = doc['content']
                if isinstance(content, dict) and 'text' in content:
                    content = content['text']
                
                formatted_docs += f"===== URL: {doc['source']} =====\n{content}\n{'-'*100}\n"
        elif retrieved_docs:
            for doc in retrieved_docs:
                content = doc['content']
                if isinstance(content, dict) and 'text' in content:
                    content = content['text']
                
                formatted_docs += f"===== URL: {doc['source']} =====\n{content}\n{'-'*100}\n"
        
        if "===== URL:" in generated_text:
            final_result = generated_text
        else:
            if formatted_docs:
                final_result = generated_text + "\n\nSource Documents:\n" + formatted_docs
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
async def stream_response_and_audio(user_query: str, kb_context, websocket: WebSocket, bart_data=None):
    if not isinstance(kb_context, str):
        kb_context = str(kb_context)
    
    is_api_related = bart_data is not None
    language = getattr(websocket.state, 'language', 'en')
    skip_tts = getattr(websocket.state, 'skip_tts', False)
    logging.info(f"Streaming response for query: '{user_query}' in language: {language}")
    logging.info(f"Query is API related: {is_api_related}")
    logging.info(f"Skip TTS: {skip_tts}")

    try:
        full_response = await generate_combined_response(user_query, kb_context, bart_data)
        
        if full_response:
            logging.info(f"Response generated: {truncate_json_for_logging(full_response, max_length=150)}")
            
            if language != 'en':
                translated_response = await translate_text(full_response, source_lang='en', target_lang=language)
                logging.info(f"Translated response: {truncate_json_for_logging(translated_response, max_length=150)}")
            else:
                translated_response = full_response
            
            if not skip_tts:
                if language in ['hi', 'zh', 'ar']:
                    phrases = chunk_text_by_words(translated_response, language=language)
                    for phrase in phrases:
                        if phrase.strip():
                            try:
                                audio_data = synthesize_speech(phrase, language=language)
                                if audio_data:
                                    await websocket.send_bytes(audio_data)
                                    logging.info(f"Streamed {len(audio_data)} bytes for phrase: {truncate_json_for_logging(phrase, 50)}")
                            except Exception as e:
                                logging.error(f"Error streaming phrase: {str(e)}")
                else:
                    sentences = re.split(r'(?<=[.!?])\s+', translated_response)
                    current_chunk = []
                    word_count = 0
                    
                    for sentence in sentences:
                        if not sentence.strip():
                            continue
                        
                        sentence_words = len(sentence.split())
                        
                        if word_count + sentence_words > 15:
                            if current_chunk:
                                chunk_text = ' '.join(current_chunk)
                                audio_data = synthesize_speech(chunk_text, language=language)
                                if audio_data:
                                    await websocket.send_bytes(audio_data)
                                current_chunk = []
                                word_count = 0
                        
                        current_chunk.append(sentence)
                        word_count += sentence_words
                        
                        if word_count >= 10:
                            chunk_text = ' '.join(current_chunk)
                            audio_data = synthesize_speech(chunk_text, language=language)
                            if audio_data:
                                await websocket.send_bytes(audio_data)
                                current_chunk = []
                                word_count = 0
                    
                    if current_chunk:
                        chunk_text = ' '.join(current_chunk)
                        audio_data = synthesize_speech(chunk_text, language=language)
                        if audio_data:
                            await websocket.send_bytes(audio_data)
            
            conversation_data = {
                "type": "conversation",
                "query": user_query,
                "response": translated_response,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": getattr(websocket.state, "session_id", None),
                "conversation_id": str(uuid.uuid4()),
                "is_final_response": True
            }
            await websocket.send_text(json.dumps(conversation_data, ensure_ascii=False))
            
            websocket.state.response_sent = True
            
            return full_response
        
        else:
            fallback_messages = {
                "en": "I'm sorry, I couldn't find specific information to answer your question about that.",
                "es": "Lo siento, no pude encontrar información específica para responder a tu pregunta sobre eso.",
                "zh": "对不起，我找不到具体信息来回答您关于那个的问题。"
            }
            
            fallback_msg = fallback_messages.get(language, fallback_messages["en"])
            logging.warning(f"No response generated, using fallback: {fallback_msg}")
            
            websocket.state.response_sent = True
            
            if not skip_tts:
                phrases = chunk_text_by_words(fallback_msg, language=language)
                for phrase in phrases:
                    audio_data = synthesize_speech(phrase, language=language)
                    if audio_data:
                        await websocket.send_bytes(audio_data)
            
            fallback_conversation_data = {
                "type": "conversation",
                "query": user_query,
                "response": fallback_msg,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": getattr(websocket.state, "session_id", None),
                "conversation_id": str(uuid.uuid4()),
                "is_final_response": True
            }
            await websocket.send_text(json.dumps(fallback_conversation_data, ensure_ascii=False))
            
            # Set response_sent flag AFTER sending the response
            websocket.state.response_sent = True
            
            return fallback_msg

    except Exception as e:
        logging.exception(f"Response generation failed: {str(e)}")
        error_messages = {
            "en": "Sorry, I couldn't get that. Try again?",
            "es": "Lo siento, no pude entender eso. ¿Intentas de nuevo?",
            "zh": "抱歉，我没听懂。请再试一次？"
        }
        error_msg = error_messages.get(language, error_messages["en"])
        
        websocket.state.response_sent = True
        
        if not skip_tts:
            fallback = synthesize_speech(error_msg, language=language)
            if fallback:
                await websocket.send_bytes(fallback)
        
        error_conversation_data = {
            "type": "conversation",
            "query": user_query,
            "response": error_msg,
            "timestamp": datetime.utcnow().isoformat(),
            "session_id": getattr(websocket.state, "session_id", None),
            "conversation_id": str(uuid.uuid4()),
            "is_final_response": True
        }
        await websocket.send_text(json.dumps(error_conversation_data, ensure_ascii=False))
        
        websocket.state.response_sent = True
        
        return error_msg

# ======================================LLM API FUNCTIONS======================================
async def non_streaming_claude_response(request_body):
    try:
        model_id = request_body.get("model_id", MODEL_ID)
        
        print("\n========== CLAUDE API REQUEST ==========")
        print(f"Model ID: {model_id}")
        print(f"Max tokens: {request_body.get('max_tokens', 1000)}")
        print(f"Temperature: {request_body.get('temperature', 0.7)}")
        print("========================================\n")
        
        start_time = asyncio.get_event_loop().time()
        
        request_info = {
            "model": model_id,
            "max_tokens": request_body.get("max_tokens", 1000),
            "temperature": request_body.get("temperature", 0.7),
            "anthropic_version": request_body.get("anthropic_version", "bedrock-2023-05-31"),
            "prompt_length": len(json.dumps(request_body.get("messages", [])))
        }
        logging.info(f"Making Claude API call: {truncate_json_for_logging(request_info, max_length=500)}")
        
        response = bedrock_client.invoke_model(
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
                    
                    print("\n========== CLAUDE API RESPONSE ==========")
                    print(f"Response time: {latency:.2f}s")
                    print(f"Response length: {len(text)} chars")
                    print("=========================================\n")
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

def synthesize_speech(text: str, language: str = "en") -> bytes:
    """Convert text to speech using Amazon Polly with language support and enhanced SSML"""
    try:
        print("\n========== SPEECH SYNTHESIS ==========")
        print(f"Text length: {len(text)} chars, Language: {language}")
        print("=====================================\n")
        
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
        
        response = polly_client.synthesize_speech(
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
    """Initialize data on startup"""
    logging.info("Application started with in-memory station data")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


async def generate_combined_response(query: str, kb_context: str, bart_data: dict = None, user_id: str = None, session_id: str = None, intent_type: str = "MIXED", language: str = "en") -> str:
    """Generate a combined response using knowledge base, BART API data, and previous context."""
    try:
        logging.info("==== GENERATING COMBINED RESPONSE ====")
        logging.info(f"Query: {query}")
        logging.info(f"Language: {language}")
        
        previous_context = "No previous context available."
        if user_id and session_id:
            prev_conversations = await get_previous_conversations(user_id, session_id)
            previous_context = await format_previous_conversations(prev_conversations)
        
        api_has_error = False
        api_no_data = False
        error_message = None
        
        if bart_data:
            if isinstance(bart_data, dict):
                if "error" in bart_data:
                    api_has_error = True
                    error_message = bart_data["error"]
                    logging.info(f"API error detected: {error_message}")
                elif "root" in bart_data and isinstance(bart_data["root"], dict) and "message" in bart_data["root"]:
                    if isinstance(bart_data["root"]["message"], dict) and "warning" in bart_data["root"]["message"]:
                        warning = bart_data["root"]["message"]["warning"]
                        if "No data matched your criteria" in warning:
                            api_no_data = True
                            error_message = warning
                            logging.info(f"API returned no matching data: {warning}")
            else:
                api_has_error = True
                error_message = f"Unexpected API response format: {type(bart_data)}"
                logging.warning(error_message)
        elif intent_type == "API":
            api_has_error = True
            error_message = "Failed to retrieve API data"
            logging.warning("API intent detected but no API data returned")
        
        if intent_type == "API":
            if bart_data:
                if api_has_error or api_no_data:
                    kb_context_to_use = "IMPORTANT: This is an API query but the API returned no data or an error. Please inform the user clearly and use the knowledge base data to provide a helpful response if relevant information is available."
                    if error_message:
                        kb_context_to_use += f"\n\nAPI Error/Warning: {error_message}"
                    logging.info("API error or no data. Explicitly directing model to use KB data.")
                else:
                    kb_context_to_use = "IMPORTANT: This is an API query. Use API data as the primary source, but supplement with knowledge base data if API data is incomplete or doesn't fully answer the query."
                    logging.info("API data available. Using as primary source but allowing KB supplementation.")
                bart_data_to_use = bart_data
            else:
                kb_context_to_use = kb_context
                bart_data_to_use = {"note": "No real-time API data available for this query"}
                logging.info("API intent detected but no API data available. Using KB as fallback.")
        
        elif intent_type == "KB":
            kb_context_to_use = kb_context
            bart_data_to_use = bart_data
            logging.info("KB intent detected. Prioritizing knowledge base data over API data.")
        else: 
            kb_context_to_use = kb_context
            bart_data_to_use = bart_data
            logging.info("Mixed intent detected. Using both API and KB data sources.")
            
        api_prioritization_note = ""
        if intent_type == "API":
            if api_has_error or api_no_data:
                api_prioritization_note = """
                CRITICAL DATA SOURCE SELECTION:
                This is an API query, but the API returned no data or an error.
                ALWAYS check the knowledge base for related information that could help answer the query.
                If the knowledge base has relevant information, use it to provide a helpful response.
                NEVER make up or fabricate any information - be honest about limitations while being helpful.
                Search for related keywords in both API data and knowledge base to provide the most relevant information available.
                IMPORTANT: For API queries where the API returns no data or errors, the knowledge base becomes your PRIMARY source of information.
                Thoroughly check all knowledge base content for information that could answer the user's query.
                If the query asks about a specific feature that isn't mentioned in either source:
                - If the information is found in EITHER source, present that information completely without mentioning where it came from
                - ONLY if the information is truly not present in EITHER source, use a friendly, conversational tone like "Looks like [feature] information for [station] isn't available right now"
                - NEVER mention "data", "API", "knowledge base", "real-time data", or any other reference to your information sources
                - Respond as if you're a helpful friend having a casual conversation
                - DO NOT assume features exist if they aren't explicitly mentioned
                - DO NOT apply general rates or policies to specialized features unless explicitly stated
                - NEVER infer information that isn't directly stated in the data sources
                Your final response MUST be derived ONLY from information explicitly present in either the API data or knowledge base.
                For station-specific queries, if the specific feature isn't mentioned for that station, acknowledge the limitation while offering related helpful information if available.
                """
            else:
                api_prioritization_note = """
                CRITICAL DATA SOURCE SELECTION:
                This is an API query that requires real-time information.
                PRIMARILY use the real-time API data in your response.
                If the API data is complete, use it exclusively.
                If the API data is missing specific information the user asked about, check the knowledge base for relevant supplementary information.
                If the API data indicates "no data matched" or contains an error, check the knowledge base for related information and use friendly conversational language to provide a helpful response.
                IMPORTANT: Even when API data is available, always check if the knowledge base contains additional relevant information that would make your response more complete.
                For queries about SPECIFIC FEATURES that aren't mentioned in the API data:
                - Check the knowledge base for relevant information about that feature
                - If the information is found in EITHER source, present that information completely without mentioning where it came from
                - ONLY if the information is truly not present in EITHER source, use a friendly, conversational tone like "Looks like [feature] information for [station] isn't available right now"
                - NEVER mention "data", "API", "knowledge base", "real-time data", or any other reference to your information sources
                - Respond as if you're a helpful friend having a casual conversation
                - DO NOT assume features exist if they aren't explicitly mentioned
                - NEVER infer information that isn't directly stated in the data sources
                - NEVER assume that general rates apply to specialized features
                Your final response MUST be derived ONLY from information explicitly present in either the API data or knowledge base.
                """
        elif intent_type == "KB":
            api_prioritization_note = """
            CRITICAL DATA SOURCE SELECTION:
            This is a KB query that requires general knowledge information.
            PRIMARILY use the knowledge base data in your response.
            If the knowledge base contains comprehensive information, use it exclusively.
            If the knowledge base lacks specific information the user asked about, check the API data for relevant supplementary information.
            If the information is found in EITHER source, present that information completely without mentioning where it came from.
            ONLY if the information is truly not present in EITHER source, use a friendly, conversational tone like "Looks like [topic] information isn't available right now".
            For questions about policies, general information, history, etc., prioritize knowledge base information.
            NEVER mention "data", "API", "knowledge base", "real-time data", or any other reference to your information sources.
            Respond as if you're a helpful friend having a casual conversation.
            Your final response MUST be derived ONLY from information explicitly present in either the knowledge base or API data.
            """
        
        language_instructions = {
            "en": """
            Please respond in English.
            Keep your responses direct, concise, and in natural-sounding English.
            """,
            "es": """
            Por favor, responde en español.
            Mantén tus respuestas directas, concisas y en español natural.
            Si se proporciona información técnica en inglés, tradúcela al español en tu respuesta.
            """,
            "zh": """
            请用中文回答。
            保持您的回答直接、简洁，并使用自然流畅的中文。
            如果提供了英文的技术信息，请在回答中将其翻译成中文。
            """
        }
        
        prompt = prompt_template.format(
            query=query,
            previous_conversations=previous_context,
            context=kb_context_to_use,
            bart_data=json.dumps(bart_data_to_use) if bart_data_to_use else "No BART API data available",
            intent_type=intent_type
        )
        
        language_instruction = language_instructions.get(language, language_instructions["en"])
        prompt = prompt + "\n" + language_instruction + "\n" + api_prioritization_note
        
        logging.info("==== PROMPT TO CLAUDE (TRUNCATED) ====")
        logging.info(f"{truncate_json_for_logging(prompt, max_length=300)}")
        logging.info("=========================")
        
        base_system_prompt = COMBINED_RESPONSE_PROMPT
        
        if intent_type == "API":
            if api_has_error or api_no_data:
                system_prompt = base_system_prompt + PROMPT1
            else:
                system_prompt = base_system_prompt + PROMPT2
        elif intent_type == "KB":
            system_prompt = base_system_prompt + PROMPT3
        else:
            system_prompt = base_system_prompt + PROMPT4
        
        temperature_value = 0.1 if intent_type == "API" else 0.2  
        
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,  
            "temperature": temperature_value,
            "system": system_prompt,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        }
        
        logging.info("Making Claude API call for final response")
        response = await non_streaming_claude_response(request_body)
        
        if response:
            logging.info(f"Claude response received: {truncate_json_for_logging(response, max_length=150)}")
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
            
        await create_or_update_user(
            user_id=user_id,
            email=user_data.get('email')
        )
        return {"message": "User created/updated successfully"}
    except Exception as e:
        logging.error(f"Error creating user: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/users/{user_id}/conversations")
async def get_user_conversation_history(user_id: str, limit: int = 10):
    try:
        conversations = await get_user_conversations(user_id, limit)
        return {"conversations": conversations}
    except Exception as e:
        logging.error(f"Error getting user conversations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/users/{user_id}")
async def get_user(user_id: str):
    try:
        response = bart_table.get_item(
            Key={
                'username': user_id,
                'record_type': f'profiles/{user_id}'
            }
        )
        user = response.get('Item')
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user
    except Exception as e:
        logging.error(f"Error getting user: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str):
    try:
        bart_table.delete_item(
            Key={
                'username': user_id,
                'record_type': f'profiles/{user_id}'
            }
        )
        
        conversations = await get_user_conversations(user_id, limit=1000)
        with bart_table.batch_writer() as batch:
            for conv in conversations:
                batch.delete_item(
                    Key={
                        'username': conv['username'],
                        'record_type': conv['record_type']
                    }
                )
        
        return {"message": "User and associated data deleted successfully"}
    except Exception as e:
        logging.error(f"Error deleting user: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    try:
        response = bart_table.query(
            KeyConditionExpression='username = :uid AND begins_with(record_type, :conv)',
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
        logging.error(f"Error getting conversation: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    try:
        response = bart_table.query(
            KeyConditionExpression='username = :uid AND begins_with(record_type, :conv)',
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
                'username': conversation['username'],
                'record_type': conversation['record_type']
            }
        )
        
        return {"message": "Conversation deleted successfully"}
    except Exception as e:
        logging.error(f"Error deleting conversation: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
   # ======================================DATABASE OPERATIONS======================================

async def save_conversation(user_id: str, query: str, response: str, audio_duration: float = None, session_id: str = None):
    """Save a conversation record in the users folder with chat history."""
    try:
        print("\n========== SAVING CONVERSATION TO DB ==========")
        print(f"User ID: {user_id}")
        print(f"Session ID: {session_id}")
        print(f"Query length: {len(query)} chars")
        print(f"Response length: {len(response)} chars")
        print("=============================================\n")
        
        conversation_id = str(uuid.uuid4())
        timestamp = datetime.now(pytz.timezone('US/Pacific')).isoformat()
        
        if not session_id:
            raise ValueError("No session_id provided")
        
        chat_id = datetime.now(pytz.timezone('US/Pacific')).strftime("%H%M%S")
        
        final_response = response
        if "Final Combined Response:" in response:
            final_response = response.split("Final Combined Response:", 1)[1].strip()
            if "--------" in final_response:
                final_response = final_response.split("--------", 1)[1].strip()
        
        item = {
            'username': user_id,
            'record_type': f'users/{user_id}/{session_id}/{chat_id}',
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
            logging.info(f"Saved conversation for user {user_id} in session {session_id}, chat {chat_id}")
            return conversation_id
        except Exception as table_error:
            return handle_dynamodb_error("save_conversation", table_error)
            
    except Exception as e:
        return handle_dynamodb_error("save_conversation", e)

async def get_user_conversations(user_id: str, limit: int = 10):
    """Get conversations for a user from the users folder."""
    try:
        print("\n========== FETCHING CONVERSATION HISTORY ==========")
        print(f"User ID: {user_id}")
        print(f"Limit: {limit}")
        print("==================================================\n")
        
        response = bart_table.query(
            KeyConditionExpression='username = :uid AND begins_with(record_type, :users)',
            ExpressionAttributeValues={
                ':uid': user_id,
                ':users': f'users/{user_id}/'
            },
            ScanIndexForward=False,
            Limit=limit
        )
        return response.get('Items', [])
    except Exception as e:
        handle_dynamodb_error("get_user_conversations", e)
        return []

async def create_or_update_user(user_id: str, email: str = None):
    """Create or update a user record in the profiles folder."""
    try:
        timestamp = datetime.utcnow().isoformat()
        
        profile_item = {
            'username': user_id,
            'record_type': f'profiles/{user_id}',
            'user_id': user_id,
            'email': email,
            'last_active': timestamp
        }
        
        logging.info(f"Creating/updating user profile for {user_id} with email: {email}")
            
        try:
            bart_table.put_item(Item=profile_item)
            logging.info(f"Successfully updated user profile for {user_id} with email {email}")
            return True
        except Exception as table_error:
            logging.error(f"Failed to update user profile: {str(table_error)}")
            return handle_dynamodb_error("create_or_update_user", table_error)
            
    except Exception as e:
        logging.error(f"Error in create_or_update_user: {str(e)}")
        return handle_dynamodb_error("create_or_update_user", e)
   # ======================================CONVERSATION FEEDBACK ENDPOINT======================================
@app.post("/api/conversations/{conversation_id}/feedback")
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
                'username': conversation['username'],
                'record_type': conversation['record_type']
            },
            UpdateExpression='SET feedback = :fb',
            ExpressionAttributeValues={
                ':fb': feedback_value
            }
        )
        
        return {"message": "Feedback saved successfully"}
    except Exception as e:
        logging.error(f"Error saving feedback: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
   # ======================================CONVERSATION HISTORY FUNCTIONS======================================
async def get_previous_conversations(user_id: str, session_id: str, limit: int = 5) -> list:
    """Get the last 5 conversations from the same session for context."""
    try:
        print("\n========== FETCHING CONVERSATION HISTORY ==========")
        print(f"User ID: {user_id}")
        print(f"Session ID: {session_id}")
        print(f"Limit: {limit}")
        print("==================================================\n")
        
        response = bart_table.query(
            KeyConditionExpression='username = :uid AND begins_with(record_type, :session_prefix)',
            ExpressionAttributeValues={
                ':uid': user_id,
                ':session_prefix': f'users/{user_id}/{session_id}/'
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
    
    context = ["IMPORTANT: Previous conversations are ONLY for understanding context. For API-related queries (schedules, train times, etc.), ALWAYS use current API data and IGNORE previous answers completely."]
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
    
    context.append("CRITICAL REMINDER: For any API-related questions about train times, schedules, or real-time information, ONLY use the current API data in your response. Previous answers contain outdated information and MUST NOT influence your current answer content.")
    
    return "\n".join(context)

# ======================================DISAMBIGUATION FUNCTIONS======================================
def extract_station_names(query_text: str) -> List[str]:
    """Extract potential station names and abbreviations from the user query."""
    import re
    potential_matches: List[str] = []
    
    station_part_mapping = {}
    
    org_terms = ["bart", "bay area rapid transit", "bay area rapid", "rapid transit"]
    
    cleaned_query = re.sub(r'[,;:]', ' ', query_text)
    cleaned_query = re.sub(r'\s+', ' ', cleaned_query).strip()
    query_lower = cleaned_query.lower()
    
    for term in org_terms:
        query_lower = re.sub(r'\b' + re.escape(term) + r'\b', '', query_lower)
    
    query_lower = re.sub(r'\s+', ' ', query_lower).strip()
    words = query_lower.split()
    
    sfo_aliases = ["sfo", "sfo airport", "san francisco airport", "sf airport", 
                  "san francisco international", "san francisco international airport"]
    
    exact_matches: List[str] = []
    abbr_matches: List[str] = []
    
    airport_phrases = [
         r'\bsan francisco airport\b', 
        r'\bsfo\b', 
        r'\bsfo airport\b',
        r'\bsf airport\b', 
        r'\bsan francisco international\b',
        r'\bsan francisco international airport\b',
        r'\bsan francisco airport\b',
        r'\bsf international airport\b',
        r'\bsf international\b'
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

    # Check for EXACT matches first - if we find an exact station match, prioritize it
    exact_station_match = False
    
    # 1. Check for exact station name matches first
    for station in STATION_DATA["stations"]:
        station_name_lower = station["name"].lower()
        
        if query_lower == station_name_lower:
            exact_matches.append(station["name"])
            exact_station_match = True
            print(f"Found exact station name match: '{station['name']}'")
            
        clean_station_name = re.sub(r'[^\w\s]', '', station["name"]).strip()
        clean_station_lower = clean_station_name.lower()
        clean_query = re.sub(r'[^\w\s]', '', query_lower).strip()
        
        if clean_query == clean_station_lower:
            exact_matches.append(station["name"])
            exact_station_match = True
            print(f"Found exact station name match (clean): '{station['name']}'")
    
    if exact_station_match:
        unique_matches = []
        for match in exact_matches:
            if match not in unique_matches:
                unique_matches.append(match)
        
        print(f"Extracted station candidates from '{query_text}' (exact station match): {unique_matches}")
        return sorted(unique_matches, key=len, reverse=True)
   
    # 2. Collect additional exact station name and abbreviation matches
    for station in STATION_DATA["stations"]:
        abbr_lower = station["abbr"].lower()
        if re.search(r'\b' + re.escape(abbr_lower) + r'\b', query_lower):
            abbr_matches.append(station["abbr"])
            exact_matches.append(station["name"])
            
    for station in STATION_DATA["stations"]:
        station_name_lower = station["name"].lower()
        
        if station_name_lower in query_lower:
            exact_matches.append(station["name"])
            continue
            
        clean_station_name = re.sub(r'[^\w\s]', '', station["name"]).strip()
        clean_station_lower = clean_station_name.lower()
        clean_query_lower = re.sub(r'[^\w\s]', '', query_lower).strip()
        
        if clean_station_lower in clean_query_lower:
            exact_matches.append(station["name"])
            continue
            
        if '/' in station["name"]:
            station_parts = station["name"].split('/')
            for part in station_parts:
                part = part.strip().lower()
                if part in query_lower:
                    exact_matches.append(station["name"])
                    print(f"Found station via slash part match: '{part}' -> '{station['name']}'")
                    station_part_mapping[part] = station["name"]
                    break

    # 3. Collect possible ambiguous/group keyword matches *in addition* to exact matches
    for group_key in STATION_GROUPS.keys():
        if group_key.lower() in query_lower:
            potential_matches.append(group_key)

    # 4. Collect partial station name matches (first 1-2 words of a station name) - with special handling for multi-part names
    for station in STATION_DATA["stations"]:
        station_parts = station["name"].split()
        
        for i in range(1, min(3, len(station_parts) + 1)):
            partial_name = " ".join(station_parts[:i])
            if len(partial_name) > 3 and partial_name.lower() in query_lower:
                potential_matches.append(station["name"])  
                print(f"Found station via partial name: '{partial_name}' -> '{station['name']}'")
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

    # 5. Handle shortened station names like "rich" for "Richmond"
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

    # 6. Handle very short queries that might be direct station name responses
    if len(exact_matches) == 0 and len(abbr_matches) == 0 and len(potential_matches) == 0 and len(query_text.split()) <= 3:
        for station in STATION_DATA["stations"]:
            for part in station["name"].split():
                if len(part) > 3 and part.lower() in query_lower:
                    potential_matches.append(station["name"])

    all_matches = []
    
    all_matches.extend(abbr_matches)
    
    for name in exact_matches:
        for station in STATION_DATA["stations"]:
            if station["name"] == name:
                if station["abbr"] not in abbr_matches:
                    all_matches.append(name)
                break
                
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
        exact_query_matches = []
        for match in unique_matches:
            if match.lower() == query_lower:
                exact_query_matches.append(match)
                print(f"Found exact query match: '{match}'")
                
        if exact_query_matches:
            print(f"Prioritizing exact matches: {exact_query_matches}")
            unique_matches = exact_query_matches

    print(
        f"Extracted station candidates from '{query_text}': {unique_matches} (exact: {exact_matches}, abbr: {abbr_matches})"
    )

    return sorted(unique_matches, key=len, reverse=True)

async def process_location_query(location_response: str, destination: str, websocket: WebSocket) -> str:
    """Process the user's location response and continue with the train query"""
    try:
        print("\n========== PROCESSING LOCATION RESPONSE ==========")
        print(f"User's location: '{location_response}'")
        print(f"Destination: '{destination}'")
        print("=================================================\n")
        
        station_names = extract_station_names(location_response)
        origin_station = None
        
        if station_names:
            origin_station = station_names[0]
            station_code = get_station_code(origin_station)
            if isinstance(station_code, tuple):
                ambiguous_options = station_code[1]
                
                print(f"Ambiguous origin station: '{origin_station}' with options: {ambiguous_options}")
                
                websocket.state.awaiting_disambiguation = True
                websocket.state.original_query = f"from {location_response} to {destination}"
                websocket.state.station_options = ambiguous_options
                websocket.state.ambiguous_station = origin_station
                
                options_text = "\n".join([f"{idx + 1}. {opt}" for idx, opt in enumerate(ambiguous_options)])
                
                language = getattr(websocket.state, 'language', 'en')
                
                disambiguation_messages = {
                    "en": f"I found multiple stations that match '{origin_station}'. Please select one:\n{options_text}",
                    "es": f"Encontré varias estaciones que coinciden con '{origin_station}'. Por favor, seleccione una:\n{options_text}",
                    "zh": f"我找到了多个与'{origin_station}'匹配的车站。请选择一个：\n{options_text}"
                }
                
                disambiguation_message = disambiguation_messages.get(language, disambiguation_messages["en"])
                
                audio_data = synthesize_speech(disambiguation_message, language=language)
                if audio_data:
                    await websocket.send_bytes(audio_data)
                
                temp_id = f"disambiguation_{uuid.uuid4()}"
                conversation_data = {
                    "type": "conversation",
                    "query": location_response,
                    "response": disambiguation_message,
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": getattr(websocket.state, "session_id", None),
                    "conversation_id": temp_id,
                    "is_disambiguation": True,
                    "display_options": True,
                    "options": [{"id": idx + 1, "name": opt} for idx, opt in enumerate(ambiguous_options)]
                }
                await websocket.send_text(json.dumps(conversation_data))
                
                return disambiguation_message
            elif station_code:
                print(f"Valid origin station: '{origin_station}' with code: {station_code}")
                
                reconstructed_query = f"next train from {origin_station} to {destination}"
                print(f"Reconstructed query: '{reconstructed_query}'")
                
                websocket.state.final_query = reconstructed_query
                
                websocket.state.awaiting_location = False
                websocket.state.destination_station = destination
                websocket.state.origin_station = origin_station
                websocket.state.complete_query = True
                
                return await process_query_and_respond_with_text(reconstructed_query, websocket)
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
        
        audio_data = synthesize_speech(message, language=lang)
        if audio_data:
            await websocket.send_bytes(audio_data)
        
        temp_id = f"location_retry_{uuid.uuid4()}"
        conversation_data = {
            "type": "conversation",
            "query": location_response,
            "response": message,
            "timestamp": datetime.utcnow().isoformat(),
            "session_id": getattr(websocket.state, "session_id", None),
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
        
        audio_data = synthesize_speech(message, language=lang)
        if audio_data:
            await websocket.send_bytes(audio_data)
        
        websocket.state.awaiting_location = False
        websocket.state.destination_station = None
        
        return message

async def process_station_disambiguation(user_response: str, options: List[str], websocket: WebSocket) -> Optional[str]:
    """Process user response to station disambiguation and return the selected station"""
    try:
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
        
        user_language = await detect_language(user_response)
        
        messages = {
            "en": "I couldn't determine which station you meant. Let's try again with your question.",
            "es": "No pude determinar qué estación quiso decir. Intentemos de nuevo con su pregunta.",
            "zh": "我无法确定您指的是哪个车站。让我们重新尝试您的问题。"
        }
        
        lang = user_language if user_language in messages else "en"
        message = messages[lang]
        
        audio_data = synthesize_speech(message, language=lang)
        if audio_data:
            await websocket.send_bytes(audio_data)
            await websocket.send_text(message)
        
        return None
    
    except Exception as e:
        logging.exception(f"Error processing station disambiguation: {str(e)}")
        return None

async def process_query_with_disambiguation(query_text: str, websocket: WebSocket) -> Optional[str]:
    """Process user query with station disambiguation if needed"""
    import re
    
    preprocessed_query = preprocess_concatenated_stations(query_text)
    
    query_text = preprocessed_query
    
    is_awaiting_disambiguation = getattr(websocket.state, 'awaiting_disambiguation', False)
    is_awaiting_location = getattr(websocket.state, 'awaiting_location', False)
    original_query = getattr(websocket.state, 'original_query', None)
    destination_station = getattr(websocket.state, 'destination_station', None)
    
    print("\n========== DISAMBIGUATION STATE ==========")
    print(f"Current query: '{query_text}'")
    print(f"Awaiting disambiguation: {is_awaiting_disambiguation}")
    print(f"Awaiting location: {is_awaiting_location}")
    print(f"Original query: '{original_query}'")
    print(f"Destination station: '{destination_station}'")
    print("=========================================\n")
    
    if is_awaiting_location and destination_station:
        return await process_location_query(query_text, destination_station, websocket)
    
    if len(query_text.split()) <= 2 and is_awaiting_disambiguation:
        print(f"Short query detected during disambiguation, treating as direct station response: '{query_text}'")
        return await process_query_and_respond_with_text(query_text, websocket)
    
    original_query_language = getattr(websocket.state, 'original_query_language', None)
    original_query_text = getattr(websocket.state, 'original_query_text', None)
    
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

    raw_extracted_stations = extract_station_names(query_text)
    print(f"Raw extracted station candidates: {raw_extracted_stations}")
    
    airport_phrases = [
        r'\bsan francisco airport\b', 
        r'\bsfo\b', 
        r'\bsfo airport\b',
        r'\bsf airport\b', 
        r'\bsan francisco international\b',
        r'\bsan francisco international airport\b',
        r'\bsan francisco airport\b',
        r'\bsf international airport\b',
        r'\bsf international\b'
    ]
    
    for phrase in airport_phrases:
        if re.search(phrase, query_lower):
            print(f"Found SFO airport mention with '{phrase}' during disambiguation")
            
            if not any(station == "San Francisco International Airport" for station in raw_extracted_stations):
                for station in STATION_DATA["stations"]:
                    if station["abbr"].lower() == "sfia":
                        if station["name"] not in raw_extracted_stations:
                            raw_extracted_stations.insert(0, station["name"])
                            print(f"Added SFIA to extracted stations list: {raw_extracted_stations}")
                        break
    
    if raw_extracted_stations:
        try:
            validation_result = validate_stations(raw_extracted_stations)
            
            if not validation_result["valid"]:
                logging.info(f"Invalid stations found: {validation_result['invalid_stations']}")
                
                updated_query = await process_station_validation(
                    query_text, 
                    validation_result["invalid_stations"],
                    validation_result["suggestions"],
                    websocket
                )
                
                if not updated_query:
                    logging.info("User did not provide valid station replacement, stopping process")
                    return None  
                
                logging.info(f"Updated query after station validation: {updated_query}")
                
                query_text = updated_query
                raw_extracted_stations = extract_station_names(query_text)
                
                query_lower = query_text.lower()
                exact_station_names_in_query = [
                    s["name"] for s in STATION_DATA["stations"] if s["name"].lower() in query_lower
                ]
                
                station_abbrs_in_query = []
                for station in STATION_DATA["stations"]:
                    abbr_lower = station["abbr"].lower()
                    if re.search(r'\b' + re.escape(abbr_lower) + r'\b', query_lower):
                        station_abbrs_in_query.append(station["abbr"])
                        if station["name"] not in exact_station_names_in_query:
                            exact_station_names_in_query.append(station["name"])
        except Exception as e:
            logging.error(f"Error in station validation: {str(e)}")
    
    station_candidates = raw_extracted_stations
    
    station_candidates = [
        cand
        for cand in station_candidates
        if cand not in exact_station_names_in_query
        and not any(cand.lower() in exact.lower() and cand.lower() != exact.lower() for exact in exact_station_names_in_query)
    ]

    print(f"Remaining station candidates to evaluate for ambiguity: {station_candidates}")

    for station_name in station_candidates:
        if len(station_name) < 3:
            continue

        if any(station_name.lower() == s["name"].lower() for s in STATION_DATA["stations"]):
            continue

        station_result = get_station_code(station_name)

        if (
            isinstance(station_result, tuple)
            and len(station_result) == 2
            and station_result[0] is None
        ):
            ambiguous_options = station_result[1]
            print("\n========== AMBIGUOUS STATION DETECTED ==========")
            print(f"Station: '{station_name}'")
            print(f"Options: {ambiguous_options}")
            print("===============================================\n")

            logging.info(
                f"Ambiguous station detected in multi-station query: '{station_name}' with options: {ambiguous_options}"
            )

            websocket.state.awaiting_disambiguation = True
            websocket.state.original_query = query_text
            websocket.state.station_options = ambiguous_options
            websocket.state.ambiguous_station = station_name

            options_text = "\n".join(
                [f"{idx + 1}. {opt}" for idx, opt in enumerate(ambiguous_options)]
            )
            
            language = getattr(websocket.state, 'language', 'en')
            
            disambiguation_messages = {
                "en": f"I found multiple stations that match '{station_name}'. Please select one:\n{options_text}",
                "es": f"Encontré varias estaciones que coinciden con '{station_name}'. Por favor, seleccione una:\n{options_text}",
                "zh": f"我找到了多个与'{station_name}'匹配的车站。请选择一个：\n{options_text}"
            }
            
            disambiguation_message = disambiguation_messages.get(language, disambiguation_messages["en"])

            audio_data = synthesize_speech(disambiguation_message, language=language)
            if audio_data:
                await websocket.send_bytes(audio_data)
            
                temp_id = f"disambiguation_{uuid.uuid4()}"
                conversation_data = {
                    "type": "conversation",
                    "query": original_query_text if original_query_text else query_text,
                    "response": disambiguation_message,
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": getattr(websocket.state, "session_id", None),
                    "conversation_id": temp_id,
                    "is_disambiguation": True,
                    "display_options": True,
                    "options": [
                        {"id": idx + 1, "name": opt} for idx, opt in enumerate(ambiguous_options)
                    ],
                }
                await websocket.send_text(json.dumps(conversation_data))
                
                websocket.state.response_sent = True
                websocket.state.last_saved_conversation = query_text

            return disambiguation_message
    
    classification_response = await non_streaming_claude_response({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": [{"type": "text", "text": QUERY_TYPE_CLASSIFICATION_PROMPT.format(query_text=query_text)}]}]
    })
    
    print("\n========== QUERY TYPE CLASSIFICATION 1==========")
    print(f"Classification1: {classification_response}")
    print("============================================\n")
    
    classification_text = classification_response.strip().lower()


    valid_types = ["greeting", "api", "kb", "stop_command", "off_topic", "off-topic"]
    if classification_text in valid_types:
        if classification_text == "off-topic":
            query_type = "off_topic"
        else:
            query_type = classification_text
    else:
        if "greeting" in classification_text:
            query_type = "greeting"
        elif "stop_command" in classification_text or "stop command" in classification_text:
            query_type = "stop_command"
        elif "api" in classification_text:
            query_type = "api"
        elif "kb" in classification_text:
            query_type = "kb"
        elif "off_topic" in classification_text or "off-topic" in classification_text:
            query_type = "off_topic"
        else:
            if "schedule" in classification_text:
                query_type = "api"
                logging.warning(f"Received 'schedule' classification, converting to 'api'")
            else:
                query_type = "kb"
                logging.warning(f"Unclear classification response: '{classification_text}', defaulting to 'kb'")
    
    print(f"Query classified as: {query_type} (from: '{classification_text}')")
    print("\n========== USER QUERY ==========")
    print(f"User Query: {query_text}")
    print("===============================\n")
    
    if query_type == 'api':
        intent_response = await non_streaming_claude_response({
             "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1000,
                    "temperature": 0.1,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": INTENT_CLASSIFICATION_PROMPT.format(query_text=query_text)}]}]
            })
        print("\n========== API INTENT CLASSIFICATION 2==========")
        print(f"Intent Response: {intent_response}")
        print("=========================================\n")
        
        try:
            import re
            json_pattern = r'(\{[\s\S]*\})'
            json_matches = re.search(json_pattern, intent_response)
            
            if json_matches:
                json_str = json_matches.group(1)
                intent_data = json.loads(json_str)
            else:
                intent_data = json.loads(intent_response)
            
            query_lower = query_text.lower()
            category = intent_data.get("category", "")
            params = intent_data.get("parameters", {})
            is_to_query = "to " in query_lower
            is_from_query = "from " in query_lower
            destination = params.get("station") or params.get("dest")
            has_origin = "orig" in params or "from " in query_lower
            if category == "REAL_TIME" and is_to_query and not is_from_query and destination and not has_origin:
                print("\n========== DESTINATION-ONLY QUERY DETECTED ==========")
                print(f"Query: '{query_text}'")
                print(f"Detected destination: '{destination}'")
                print("====================================================\n")
                websocket.state.awaiting_location = True
                websocket.state.destination_station = destination
                language = getattr(websocket.state, 'language', 'en')
                location_request = f"To find trains to {destination}, I need to know which BART station you're currently at. Please tell me your current station."
                audio_data = synthesize_speech(location_request, language=language)
                if audio_data:
                    await websocket.send_bytes(audio_data)
                    
                temp_id = f"location_request_{uuid.uuid4()}"
                conversation_data = {
                    "type": "conversation",
                    "query": query_text,
                    "response": location_request,
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": getattr(websocket.state, "session_id", None),
                    "conversation_id": temp_id,
                    "is_location_request": True
                }
                await websocket.send_text(json.dumps(conversation_data))
                
                websocket.state.response_sent = True
                websocket.state.last_saved_conversation = query_text
                return location_request
        except json.JSONDecodeError as e:
            print(f"Error parsing intent data: {e}")
    
    return await process_query_and_respond_with_text(query_text, websocket)

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
    last_saved = getattr(websocket.state, 'last_saved_conversation', None)
    final_query = getattr(websocket.state, 'final_query', None)
    is_location_flow = bool(final_query)
    
    if final_query:
        logging.info(f"Using final reconstructed query '{final_query}' instead of '{query}'")
        query = final_query
        websocket.state.final_query = None
    
    if getattr(websocket.state, 'awaiting_location', False) or getattr(websocket.state, 'awaiting_disambiguation', False):
        logging.info(f"Skipping save_and_send_response for query '{query}' - already sent as location/disambiguation request")
        websocket.state.response_sent = True
        return
        
    if last_saved is not None and (last_saved == query or query in last_saved or last_saved in query):
        logging.info(f"Conversation for query '{query}' has already been saved - skipping save")
        websocket.state.response_sent = True
        return
        
    original_query_language = getattr(websocket.state, 'original_query_language', None)
    original_query_text = getattr(websocket.state, 'original_query_text', query)
    replacement_info = getattr(websocket.state, 'replaced_query', None)
    
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
    
    conversation_data = {
        'type': 'conversation',
        'query': original_query_text if original_query_text else query,
        'response': display_response,
        'timestamp': datetime.utcnow().isoformat(),
        'session_id': session_id,
        'is_disambiguation': is_disambiguation,
        'full_response': combined_response
    }

    if replacement_info:
        conversation_data['replacement_info'] = replacement_info
        conversation_data['query'] = replacement_info['final']
        conversation_data['original_query'] = replacement_info['original']
        conversation_data['is_station_replacement'] = True
        
        websocket.state.replaced_query = None
    
    save_query = replacement_info['final'] if replacement_info else query
    
    websocket.state.last_saved_conversation = query
    
    should_save_to_db = False
    
    has_origin_dest = ('from ' in save_query.lower() and 'to ' in save_query.lower()) or is_location_flow
    
    if has_origin_dest and not getattr(websocket.state, 'awaiting_location', False) and not getattr(websocket.state, 'awaiting_disambiguation', False):
        should_save_to_db = True
        logging.info(f"Will save to database: Query '{save_query}' has both origin and destination")
    else:
        logging.info(f"Skipping database save for query: '{save_query}' - not a complete query with origin/destination")
    
    # Save to database if appropriate
    conversation_id = None
    if should_save_to_db:
        try:
            save_task = asyncio.create_task(save_conversation(
                user_id=user_id,
                query=save_query,
                response=display_response,
                session_id=session_id
            ))
            
            conversation_id = await save_task
            
            if conversation_id:
                conversation_data['conversation_id'] = conversation_id
        except Exception as e:
            logging.error(f"Error saving conversation to database: {str(e)}")
    
    await websocket.send_text(json.dumps(conversation_data, ensure_ascii=False))
    websocket.state.response_sent = True
    
    if not getattr(websocket.state, 'awaiting_disambiguation', False) and not getattr(websocket.state, 'awaiting_location', False):
        websocket.state.complete_query = False
        
        if 'orig' not in query.lower() and 'from ' not in query.lower():
            websocket.state.origin_station = None
            
        if 'dest' not in query.lower() and 'to ' not in query.lower():
            websocket.state.destination_station = None

async def translate_text(text: str, source_lang: str = "en", target_lang: str = "en") -> str:
    """Translate text between languages using AWS Translate with retry and error handling"""
    if source_lang == target_lang or not text.strip():
        return text
        
    try:
        logging.info(f"Translating text from {source_lang} to {target_lang}: {truncate_json_for_logging(text, 100)}")
        
        translate_client = boto3.client('translate', region_name="us-west-2")
        
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
            comprehend_client = boto3.client('comprehend', region_name=REGION)
            
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

def extract_date_type_from_query(query_text: str) -> Optional[str]:
    """Extract date type (weekday, saturday, sunday) from user query
    
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

def normalize_date_format(date_str: str) -> Optional[str]:
    """Normalize various date formats to mm/dd/yyyy format required by BART API
    
    Handles formats like:
    - mm/dd/yyyy
    - mm-dd-yyyy
    - yyyy-mm-dd
    - mm/dd/yy
    - today
    - now
    """
    if not date_str:
        return None
        
    date_str = date_str.lower().strip()
    
    if date_str in ['today', 'now']:
        return date_str
        
    if date_str in ['wd', 'sa', 'su']:
        return date_str
    
    weekday_map = {
        'monday': 'wd',
        'tuesday': 'wd',
        'wednesday': 'wd',
        'thursday': 'wd',
        'friday': 'wd',
        'saturday': 'sa',
        'sunday': 'su'
    }
    
    if date_str in weekday_map:
        return weekday_map[date_str]
    
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

def validate_stations(station_names: List[str]) -> Dict[str, Any]:
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
    
    sfo_aliases = ["sfo", "sfo airport", "san francisco airport", "sf airport", 
                  "san francisco international", "san francisco international airport"]
    
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

async def process_station_validation(query_text: str, invalid_stations: List[str], 
                               suggestions: Dict[str, List[str]], websocket: WebSocket) -> Optional[str]:
    """
    Process invalid station names by informing the user and requesting valid replacements.
    
    Args:
        query_text: The original query text
        invalid_stations: List of invalid station names
        suggestions: Dictionary mapping each invalid station to suggested alternatives
        websocket: WebSocket connection for communication
    
    Returns:
        Modified query with valid station names, or None if processing should stop to wait for user input
    """
    org_terms = ["bart","BART", "bay area rapid transit", "bay area rapid", "rapid transit"]
    
    query_lower = query_text.lower()
    if any(term in query_lower for term in org_terms) and any(word in query_lower for word in ["station", "stations", "stop", "stops"]):
        return query_text
    
    for invalid_station in invalid_stations:
        if invalid_station.lower() in org_terms:
            continue
            
        station_suggestions = suggestions[invalid_station]
        
        suggestion_list = ", ".join(station_suggestions[:5])  
        
        user_language = getattr(websocket.state, 'language', 'en')
        
        messages = {
            "en": {
                "invalid_station": f"'{invalid_station}' is not a valid BART station.\n\nDid you mean one of these?\n• {station_suggestions[0]}",
                "please_say": "\n\nPlease say the complete name of the station you want."
            },
            "es": {
                "invalid_station": f"'{invalid_station}' no es una estación de BART válida.\n\n¿Quiso decir una de estas?\n• {station_suggestions[0]}",
                "please_say": "\n\nPor favor, diga el nombre completo de la estación que desea."
            },
            "zh": {
                "invalid_station": f"'{invalid_station}' 不是有效的BART车站。\n\n您是否指的是以下车站之一？\n• {station_suggestions[0]}",
                "please_say": "\n\n请说出您想要的车站的完整名称。"
            }
        }
        
        lang = user_language if user_language in messages else "en"
        
        message = messages[lang]["invalid_station"]
        
        for suggestion in station_suggestions[1:5]:
            message += f"\n• {suggestion}"
            
        message += messages[lang]["please_say"]
        
        logging.info(f"Sending invalid station message with {len(station_suggestions[:5])} suggestions")
        
        websocket.state.awaiting_station_validation = True
        websocket.state.invalid_station = invalid_station
        websocket.state.station_suggestions = station_suggestions[:5]
        websocket.state.original_query = query_text
        
        print("\n========== STATION VALIDATION REQUEST ==========")
        print(f"Invalid station: '{invalid_station}'")
        print(f"Original query: '{query_text}'")
        print(f"Suggestions: {station_suggestions[:5]}")
        print("===============================================\n")
        
        conversation_data = {
            "type": "conversation",
            "conversation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(pytz.timezone('America/Los_Angeles')).isoformat(),
            "query": query_text,
            "response": message,
            "session_id": getattr(websocket.state, "session_id", None),
            "is_disambiguation": True,
            "is_invalid_station": True,
            "invalid_station": invalid_station,
            "display_suggestions": True,
            "options": station_suggestions[:5]  
        }
        
        websocket.state.last_saved_conversation = query_text
        
        try:
            await websocket.send_text(json.dumps(conversation_data))
            
            speech_messages = {
                "en": f"'{invalid_station}' is not a valid BART station. Did you mean one of these: {', '.join(station_suggestions)}? Please say the complete name of the station you want.",
                "es": f"'{invalid_station}' no es una estación de BART válida. ¿Quiso decir una de estas: {', '.join(station_suggestions)}? Por favor, diga el nombre completo de la estación que desea.",
                "zh": f"'{invalid_station}' 不是有效的BART车站。您是否指的是以下车站之一：{', '.join(station_suggestions)}？请说出您想要的车站的完整名称。"
            }
            
            speech_message = speech_messages.get(lang, speech_messages["en"])
            audio_bytes = synthesize_speech(speech_message, language=lang)
            if audio_bytes:
                await websocket.send_bytes(audio_bytes)
            
            print("Returning None from process_station_validation to await user input")
            return None
                
        except WebSocketDisconnect:
            logging.warning("WebSocket disconnected during station validation")
            return None
        except Exception as e:
            logging.error(f"Error during station validation: {str(e)}")
            return None
    
    return query_text

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
