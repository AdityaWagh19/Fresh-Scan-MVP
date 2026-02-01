"""
Recipe Hash Generation Module
Implements proper recipe fingerprinting for uniqueness detection (Fix for Issue 1)
"""

import hashlib
from typing import Dict, List


def generate_recipe_hash(recipe_name: str, ingredients: List[str], cooking_method: str = "", 
                        cuisine: str = "", dietary_tags: List[str] = None) -> str:
    """
    Generate a robust SHA-256 hash for recipe uniqueness detection.
    
    Hash is based on:
    - Sorted, normalized ingredients (lowercase, alphabetically sorted)
    - Cooking method
    - Cuisine type
    - Dietary tags
    
    This ensures recipes with same core components are detected as duplicates
    even if the name is slightly different.
    
    Args:
        recipe_name: Name of the recipe
        ingredients: List of ingredient names
        cooking_method: Cooking technique (e.g., "stir-fried", "baked")
        cuisine: Cuisine type (e.g., "Asian", "Italian")
        dietary_tags: List of dietary tags (e.g., ["vegetarian", "gluten-free"])
    
    Returns:
        SHA-256 hash string (64 characters)
    """
    if dietary_tags is None:
        dietary_tags = []
    
    # Normalize ingredients: lowercase, strip whitespace, sort alphabetically
    normalized_ingredients = sorted([ing.lower().strip() for ing in ingredients if ing])
    
    # Normalize other components
    normalized_method = cooking_method.lower().strip() if cooking_method else ""
    normalized_cuisine = cuisine.lower().strip() if cuisine else ""
    normalized_tags = sorted([tag.lower().strip() for tag in dietary_tags if tag])
    
    # Create fingerprint string
    fingerprint_parts = [
        "|".join(normalized_ingredients),
        normalized_method,
        normalized_cuisine,
        "|".join(normalized_tags)
    ]
    
    fingerprint = "::".join(fingerprint_parts)
    
    # Generate SHA-256 hash
    return hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()


def check_hash_collision(new_recipe: Dict, existing_recipe: Dict) -> bool:
    """
    Check if two recipes with matching hashes are actually different (collision detection).
    
    Uses name similarity as fallback check.
    
    Args:
        new_recipe: New recipe dict with 'name' and 'hash' keys
        existing_recipe: Existing recipe dict with 'name' and 'hash' keys
    
    Returns:
        True if collision detected (same hash but different recipes), False otherwise
    """
    if new_recipe.get('hash') != existing_recipe.get('hash'):
        return False
    
    # Hashes match - check if names are significantly different
    new_name = new_recipe.get('name', '').lower()
    existing_name = existing_recipe.get('name', '').lower()
    
    # Calculate word overlap similarity
    new_words = set(new_name.split())
    existing_words = set(existing_name.split())
    
    if not new_words or not existing_words:
        return False
    
    intersection = new_words.intersection(existing_words)
    union = new_words.union(existing_words)
    similarity = len(intersection) / len(union) if union else 0.0
    
    # If similarity < 50%, likely a collision (different recipes, same hash)
    return similarity < 0.5


def calculate_recipe_diversity_score(recipes: List[Dict]) -> float:
    """
    Calculate diversity score (0-1) for a collection of recipes.
    
    Higher score = more diverse recipes
    
    Args:
        recipes: List of recipe dicts with 'hash' keys
    
    Returns:
        Diversity score from 0.0 (all duplicates) to 1.0 (all unique)
    """
    if not recipes:
        return 0.0
    
    if len(recipes) == 1:
        return 1.0
    
    # Count unique hashes
    unique_hashes = len(set(r.get('hash', '') for r in recipes if r.get('hash')))
    
    # Diversity = unique / total
    return unique_hashes / len(recipes)
