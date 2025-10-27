import json
import logging

def truncate_json_for_logging(json_data, max_length=None):
    """NO TRUNCATION - Return full data for production session logs"""
    if json_data is None:
        return "None"
    try:
        if isinstance(json_data, (dict, list)):
            json_str = json.dumps(json_data, indent=2, ensure_ascii=False)
            return json_str 
        else:
            str_data = str(json_data)
            return str_data   
    except:
        return str(json_data) 
