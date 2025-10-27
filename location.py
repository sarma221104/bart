import boto3
import json
import logging
import math
from typing import Dict, Optional, Tuple
from datetime import datetime
import asyncio
from decimal import Decimal
from constants import station_data

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LocationManager:
    def __init__(self):
        from aws_client_manager import get_dynamodb_resource
        self.dynamodb = get_dynamodb_resource()
        import os
        self.table = self.dynamodb.Table(os.getenv("DYNAMODB_TABLE"))
    
    def calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate the distance between two points using the Haversine formula
        Returns distance in kilometers
        """
        # Convert decimal degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        
        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        # Radius of earth in kilometers
        r = 6371
        return c * r
    
    def find_nearest_station(self, user_lat: float, user_lon: float) -> Optional[Dict]:
        """
        Find the nearest BART station to the user's location
        Returns station data with name, abbreviation, and distance
        """
        if not station_data.get('stations'):
            logger.error("No station data available")
            return None
        
        nearest_station = None
        min_distance = float('inf')
        
        for station in station_data['stations']:
            if 'latitude' in station and 'longitude' in station:
                distance = self.calculate_distance(
                    user_lat, user_lon,
                    station['latitude'], station['longitude']
                )
                
                if distance < min_distance:
                    min_distance = distance
                    nearest_station = {
                        'name': station['name'],
                        'abbr': station['abbr'],
                        'distance': distance,
                        'latitude': station['latitude'],
                        'longitude': station['longitude']
                    }
        
        if nearest_station:
            logger.info(f"Nearest station: {nearest_station['name']} ({nearest_station['abbr']}) at {nearest_station['distance']:.2f} km")
        
        return nearest_station
    
    async def store_user_location(self, session_id: str, user_id: str, latitude: float, longitude: float) -> bool:
        """
        Store user location in DynamoDB with session data
        Only update if location has changed significantly (>100 meters)
        Also stores the nearest station for quick access
        Ensures all mandatory session fields are present
        """
        try:
            # Check if location already exists for this session
            response = self.table.get_item(
                Key={
                    'user_id': user_id,
                    'record_type': f'sessions/{user_id}/{session_id}'
                }
            )
            
            existing_item = response.get('Item')
            location_changed = True
            
            if existing_item:
                existing_lat = existing_item.get('user_latitude')
                existing_lon = existing_item.get('user_longitude')
                
                if existing_lat is not None and existing_lon is not None:
                    # Calculate distance between existing and new location
                    distance = self.calculate_distance(
                        float(existing_lat), float(existing_lon),
                        latitude, longitude
                    )
                    
                    # Only update if location changed by more than 100 meters
                    if distance < 0.1:  # 0.1 km = 100 meters
                        location_changed = False
                        logger.info(f"Location hasn't changed significantly (distance: {distance:.3f} km)")
            
            if location_changed:
                # Find nearest station
                nearest_station = self.find_nearest_station(latitude, longitude)
                
                # Check if session has all mandatory fields
                mandatory_fields = ['created_at', 'is_active', 'language', 'last_active', 'session_id', 'ttl']
                missing_fields = [field for field in mandatory_fields if field not in existing_item]
                
                if missing_fields:
                    logger.warning(f"Session {session_id} is missing mandatory fields: {missing_fields}")
                    logger.warning("This session was not created properly. Location update may fail.")
                
                # Update the session record with new location and nearest station
                update_expression = "SET user_latitude = :lat, user_longitude = :lon, last_updated = :timestamp"
                expression_values = {
                    ':lat': Decimal(str(latitude)),
                    ':lon': Decimal(str(longitude)),
                    ':timestamp': datetime.now().isoformat()
                }
                
                # Add nearest station info if found
                if nearest_station:
                    update_expression += ", nearest_station_name = :station_name, nearest_station_abbr = :station_abbr, nearest_station_distance = :station_distance"
                    expression_values.update({
                        ':station_name': nearest_station['name'],
                        ':station_abbr': nearest_station['abbr'],
                        ':station_distance': Decimal(str(nearest_station['distance']))
                    })
                    logger.info(f"Stored nearest station: {nearest_station['name']} ({nearest_station['abbr']}) at {nearest_station['distance']:.2f} km")
                
                self.table.update_item(
                    Key={
                        'user_id': user_id,
                        'record_type': f'sessions/{user_id}/{session_id}'
                    },
                    UpdateExpression=update_expression,
                    ExpressionAttributeValues=expression_values
                )
                
                logger.info(f"Stored new location for session {session_id}: {latitude}, {longitude}")
                return True
            else:
                return False
                
        except Exception as e:
            logger.error(f"Error storing user location: {str(e)}")
            return False
    
    async def get_user_location(self, session_id: str, user_id: str) -> Optional[Tuple[float, float]]:
        """
        Retrieve user location from DynamoDB
        Returns tuple of (latitude, longitude) or None if not found
        """
        try:
            response = self.table.get_item(
                Key={
                    'user_id': user_id,
                    'record_type': f'sessions/{user_id}/{session_id}'
                }
            )
            
            item = response.get('Item')
            if item and 'user_latitude' in item and 'user_longitude' in item:
                return (float(item['user_latitude']), float(item['user_longitude']))
            
            return None
            
        except Exception as e:
            logger.error(f"Error retrieving user location: {str(e)}")
            return None
    
    async def get_nearest_station_for_session(self, session_id: str, user_id: str) -> Optional[Dict]:
        """
        Get the nearest BART station for a user's session
        Returns station data or None if location not available
        """
        try:
            # First try to get from stored nearest station data
            response = self.table.get_item(
                Key={
                    'user_id': user_id,
                    'record_type': f'sessions/{user_id}/{session_id}'
                }
            )
            
            item = response.get('Item')
            if item and 'nearest_station_abbr' in item:
                # Return stored nearest station data
                return {
                    'name': item.get('nearest_station_name', ''),
                    'abbr': item.get('nearest_station_abbr', ''),
                    'distance': float(item.get('nearest_station_distance', 0)),
                    'latitude': item.get('user_latitude'),
                    'longitude': item.get('user_longitude')
                }
            
            # Fallback: calculate from stored location
            location = await self.get_user_location(session_id, user_id)
            if location:
                lat, lon = location
                return self.find_nearest_station(lat, lon)
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting nearest station for session: {str(e)}")
            return None

# Global instance
location_manager = LocationManager()
