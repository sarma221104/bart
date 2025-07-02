from fastapi import FastAPI
from typing import Optional, Dict, Any, List, Tuple
import logging
from datetime import datetime

app = FastAPI()

# ======================================BART API ENDPOINTS======================================
# ======================================ADVISORY ENDPOINTS======================================
@app.get("/api/bart/bsa")
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
    
@app.get("/api/bart/count")
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

@app.get("/api/bart/ets")
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
@app.get("/api/bart/etd")
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
@app.get("/api/bart/stn")
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

@app.get("/api/bart/stations")
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

@app.get("/api/bart/station/access")
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
@app.get("/api/bart/route")
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
    
@app.get("/api/bart/routes")
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
@app.get("/api/bart/schedule/route")
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
    
@app.get("/api/bart/sched/list")
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

@app.get("/api/bart/fare")
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

@app.get("/api/bart/schedule/arrivals")
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

@app.get("/api/bart/schedule/departures")
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

@app.get("/api/bart/schedule/station")
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