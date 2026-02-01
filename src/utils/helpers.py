import os
import re
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Callable, Any, Optional
from functools import wraps
from src.config.constants import INGREDIENT_SYNONYMS
#     'dairy': ['milk', 'cheese', 'yogurt'],

def log_error(context: str, e: Exception) -> None:
    """Standardized error logging."""
    print(f"Error in {context}: {str(e)}")
    logging.error(f"Error in {context}: {str(e)}", exc_info=True)

def safe_db_call(action_desc: str, operation: Callable) -> Optional[Any]:
    """Wrapper for safe database operations with error handling."""
    try:
        return operation()
    except Exception as e:
        log_error(f"{action_desc} (DB operation)", e)
        return None

def get_multiple_choice(prompt: str, options: Dict[str, str], allow_blank=True) -> List[str]:
    """Generalized multiple choice input handler."""
    print(f"\n{prompt}\n(comma separated, or leave blank for none)")
    
    print("Options:")
    for key, value in options.items():
        print(f"{key}. {value}")
   
    choices = []
    while True:
        user_input = input("Select (comma separated): ").strip()
        if not user_input and allow_blank:
            return []
       
        invalid = False
        for choice in user_input.split(','):
            choice = choice.strip()
            if choice not in options:
                print(f"Invalid choice: {choice}\nValid options are: {', '.join(options.keys())}")
                invalid = True
                break
       
        if not invalid:
            break
   
    return [options[choice.strip()] for choice in user_input.split(',') if choice.strip() in options]

def input_with_default(prompt: str, default: Any) -> str:
    """Input handler with default values."""
    print(f"\n{prompt}\n(default: {default})")
    user_input = input("> ").strip()
    return user_input if user_input else str(default)

def is_cache_valid(timestamp: str, ttl_hrs=24) -> bool:
    """Check if cached data is still valid."""
    try:
        cache_time = datetime.fromisoformat(timestamp)
        return (datetime.now() - cache_time) < timedelta(hours=ttl_hrs)
    except:
        return False

def normalize_ingredient_name(name: str) -> str:
    """Normalize ingredient names using regex patterns."""
    if not name:
        return ""
       
    name = name.lower().strip()
   
    # Remove quantity descriptors and units
    name = re.sub(r'\b\d+\s*(kg|g|ml|l|oz|lb|pound|ounce|liter|gram|kilo)\b', '', name)
    name = re.sub(r'\b(pieces?|chopped|halves|whole|sliced|diced|minced|grated)\b', '', name)
   
    # Remove parenthetical quantities and notes
    name = re.sub(r'\(.*?\)', '', name)
   
    # Remove special characters except hyphens between words
    name = re.sub(r'[^\w\s-]', '', name)
   
    # Clean up whitespace
    name = re.sub(r'\s+', ' ', name).strip()
   
    # Apply synonym mapping
    for original, synonym in INGREDIENT_SYNONYMS.items():
        if original in name:
            name = name.replace(original, synonym)
            break  # Only replace one synonym per item
   
    return name