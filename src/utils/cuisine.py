"""
Intelligent Cuisine Detection Module
Fixes hardcoded 'Indian' fallback with location-aware and ingredient-based detection (Fix for Issue 3)
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)


def determine_cuisine_intelligent(
    components: Dict,
    context: Dict,
    default_cuisine: str = None
) -> str:
    """
    Intelligently determine cuisine with multi-level fallback strategy.
    
    Fallback order:
    1. User's explicit cuisine preferences (from profile)
    2. Ingredient pattern detection (e.g., soy sauce → Asian)
    3. Location-based inference (from timezone/location in profile)
    4. Configurable default (from constants.py)
    5. Final fallback: 'International'
    
    Args:
        components: Recipe components dict with vegetables, flavor, sauce, etc.
        context: User context dict with preferences, location, etc.
        default_cuisine: Configurable default from constants.py
    
    Returns:
        Detected cuisine string
    """
    # Level 1: User's explicit preferences (80% chance to use if available)
    preferred_cuisines = context.get('cuisine_preferences', [])
    if preferred_cuisines:
        import random
        if random.random() < 0.8:
            cuisine = random.choice(preferred_cuisines)
            logger.info(f"Using user preference: {cuisine}")
            return cuisine
    
    # Level 2: Ingredient pattern detection
    ingredient_cuisine = _detect_cuisine_from_ingredients(components)
    if ingredient_cuisine:
        logger.info(f"Detected from ingredients: {ingredient_cuisine}")
        return ingredient_cuisine
    
    # Level 3: Location-based inference
    location_cuisine = _infer_cuisine_from_location(context)
    if location_cuisine:
        logger.info(f"Inferred from location: {location_cuisine}")
        return location_cuisine
    
    # Level 4: Configurable default
    if default_cuisine:
        logger.info(f"Using configured default: {default_cuisine}")
        return default_cuisine
    
    # Level 5: Final fallback
    logger.warning("Using final fallback: International")
    return "International"


def _detect_cuisine_from_ingredients(components: Dict) -> Optional[str]:
    """
    Detect cuisine type from ingredient patterns.
    
    Args:
        components: Recipe components with vegetables, flavor, sauce, protein
    
    Returns:
        Detected cuisine or None
    """
    vegetables = components.get('vegetables', '').lower()
    flavor = components.get('flavor', '').lower()
    sauce = components.get('sauce', '').lower()
    protein = components.get('protein', '').lower()
    
    # Combine all text for pattern matching
    all_text = f"{vegetables} {flavor} {sauce} {protein}"
    
    # Asian cuisine patterns
    asian_keywords = ['soy', 'ginger', 'sesame', 'teriyaki', 'miso', 'wasabi', 
                     'rice vinegar', 'hoisin', 'oyster sauce', 'fish sauce']
    if any(keyword in all_text for keyword in asian_keywords):
        # Further refine Asian cuisine
        if 'miso' in all_text or 'wasabi' in all_text:
            return 'Japanese'
        elif 'fish sauce' in all_text or 'lemongrass' in all_text:
            return 'Thai'
        elif 'szechuan' in all_text or 'five-spice' in all_text:
            return 'Chinese'
        else:
            return 'Asian'
    
    # Indian cuisine patterns
    indian_keywords = ['curry', 'masala', 'turmeric', 'cumin', 'coriander', 
                      'garam', 'tandoori', 'paneer', 'naan', 'biryani']
    if any(keyword in all_text for keyword in indian_keywords):
        return 'Indian'
    
    # Mediterranean cuisine patterns
    mediterranean_keywords = ['olive', 'oregano', 'basil', 'feta', 'hummus', 
                             'tahini', 'za\'atar', 'sumac', 'pita']
    if any(keyword in all_text for keyword in mediterranean_keywords):
        return 'Mediterranean'
    
    # Italian cuisine patterns
    italian_keywords = ['pasta', 'parmesan', 'mozzarella', 'pesto', 'marinara', 
                       'risotto', 'linguine', 'carbonara']
    if any(keyword in all_text for keyword in italian_keywords):
        return 'Italian'
    
    # Mexican cuisine patterns
    mexican_keywords = ['salsa', 'cilantro', 'lime', 'jalapeño', 'chipotle', 
                       'tortilla', 'guacamole', 'queso', 'taco']
    if any(keyword in all_text for keyword in mexican_keywords):
        return 'Mexican'
    
    # French cuisine patterns
    french_keywords = ['herb', 'thyme', 'rosemary', 'béchamel', 'roux', 
                      'coq au vin', 'bourguignon']
    if any(keyword in all_text for keyword in french_keywords):
        return 'French'
    
    return None


def _infer_cuisine_from_location(context: Dict) -> Optional[str]:
    """
    Infer likely cuisine preference from user's location/timezone.
    
    Args:
        context: User context with location or timezone info
    
    Returns:
        Inferred cuisine or None
    """
    # Try to get location from context
    location = context.get('location', '').lower()
    timezone_str = context.get('timezone', '')
    
    # Location-based mapping
    if location:
        if any(country in location for country in ['india', 'indian', 'mumbai', 'delhi', 'bangalore']):
            return 'Indian'
        elif any(country in location for country in ['china', 'chinese', 'beijing', 'shanghai']):
            return 'Chinese'
        elif any(country in location for country in ['japan', 'japanese', 'tokyo', 'osaka']):
            return 'Japanese'
        elif any(country in location for country in ['italy', 'italian', 'rome', 'milan']):
            return 'Italian'
        elif any(country in location for country in ['mexico', 'mexican']):
            return 'Mexican'
        elif any(country in location for country in ['france', 'french', 'paris']):
            return 'French'
        elif any(country in location for country in ['thailand', 'thai', 'bangkok']):
            return 'Thai'
    
    # Timezone-based inference (rough approximation)
    if timezone_str:
        try:
            # Asia/Kolkata → Indian
            if 'kolkata' in timezone_str.lower() or 'calcutta' in timezone_str.lower():
                return 'Indian'
            # Asia/Tokyo → Japanese
            elif 'tokyo' in timezone_str.lower():
                return 'Japanese'
            # Asia/Shanghai → Chinese
            elif 'shanghai' in timezone_str.lower() or 'hong_kong' in timezone_str.lower():
                return 'Chinese'
            # Europe/Rome → Italian
            elif 'rome' in timezone_str.lower():
                return 'Italian'
            # Europe/Paris → French
            elif 'paris' in timezone_str.lower():
                return 'French'
            # America/Mexico_City → Mexican
            elif 'mexico' in timezone_str.lower():
                return 'Mexican'
        except Exception as e:
            logger.error(f"Error parsing timezone: {e}")
    
    return None


def get_default_cuisine_from_config() -> str:
    """
    Get default cuisine from constants.py configuration.
    
    Returns:
        Configured default cuisine or 'International'
    """
    try:
        from src.config.constants import DEFAULT_CUISINE
        return DEFAULT_CUISINE
    except ImportError:
        logger.warning("DEFAULT_CUISINE not found in constants.py, using 'International'")
        return "International"
