"""
Merged module from:
  - src/recipes/youtube_helper.py
  - src/recipes/youtube_quota.py
"""

# Imports
from datetime import datetime, timedelta
from src.database.connection import DatabaseConnectionContext
from src.database.transactions import MongoTransaction, TransactionManager
from typing import List, Dict, Optional
from typing import Optional, List
import logging
import re


# ===== From src/recipes/youtube_helper.py =====

def optimize_youtube_query(recipe_name: str, ingredients: Optional[List[str]] = None) -> str:
    """
    Clean and optimize recipe name for better YouTube search results.
    Optionally includes key ingredients for more accurate matching.
    
    Examples:
        "Spanish Garlic & Paprika Toasts (Pan con Ajo y Pimentón)" 
        -> "Spanish Garlic Paprika Toasts recipe"
        
        "Spicy Garlic Ginger Tofu Noodles", ingredients=["tofu", "noodles"]
        -> "tofu noodles recipe"
    
    Args:
        recipe_name: Full recipe name from AI generation
        ingredients: Optional list of key ingredients to include in search
        
    Returns:
        Optimized search query for YouTube
    """
    # If we have ingredients, use them for a more targeted search
    if ingredients and len(ingredients) > 0:
        # Extract 2-3 most important ingredients (skip common ones)
        common_ingredients = {'salt', 'pepper', 'oil', 'water', 'butter', 'flour', 'sugar'}
        key_ingredients = [ing for ing in ingredients if ing.lower() not in common_ingredients][:3]
        
        if key_ingredients:
            # Create query from ingredients
            search_query = f"{' '.join(key_ingredients)} recipe"
            return search_query
    
    # Fallback to recipe name cleaning
    # Remove parenthetical translations and extra details
    clean_name = re.sub(r'\([^)]*\)', '', recipe_name).strip()
    
    # Remove special characters that might confuse search
    clean_name = clean_name.replace('&', 'and')
    
    # Take only the first part if there's a dash or colon (removes subtitles)
    if ' - ' in clean_name:
        clean_name = clean_name.split(' - ')[0].strip()
    if ': ' in clean_name:
        clean_name = clean_name.split(': ')[0].strip()
    
    # Limit to first 5 words for better matching
    # YouTube search works better with concise queries
    words = clean_name.split()
    if len(words) > 5:
        clean_name = ' '.join(words[:5])
    
    # Add "recipe" keyword for better results
    search_query = f"{clean_name} recipe"
    
    return search_query

# ===== From src/recipes/youtube_quota.py =====

logger = logging.getLogger(__name__)

# YouTube API costs (in quota units)
SEARCH_COST = 100
VIDEO_DETAILS_COST = 1
DAILY_QUOTA_LIMIT = 10000


class YouTubeQuotaManager:
    """Manages YouTube API quota tracking and caching."""
    
    def __init__(self, database):
        self.database = database
        self.cache_duration_days = 7
    
    def get_remaining_quota(self) -> int:
        """
        Get remaining quota for today.
        
        Returns:
            Remaining quota units (0 if quota exceeded)
        """
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                
                # Get today's usage
                quota_doc = db['youtube_quota'].find_one({
                    'date': today
                })
                
                if not quota_doc:
                    # No usage today
                    return DAILY_QUOTA_LIMIT
                
                used = quota_doc.get('units_used', 0)
                remaining = max(0, DAILY_QUOTA_LIMIT - used)
                
                logger.info(f"YouTube quota: {used}/{DAILY_QUOTA_LIMIT} used, {remaining} remaining")
                return remaining
                
        except Exception as e:
            logger.error(f"Error checking quota: {e}")
            # Return 0 to be safe
            return 0
    
    def record_api_call(self, cost: int) -> bool:
        """
        Record an API call and deduct from quota.
        
        Args:
            cost: Quota units consumed by this call
        
        Returns:
            True if recorded successfully, False otherwise
        """
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                
                # Increment usage
                db['youtube_quota'].update_one(
                    {'date': today},
                    {
                        '$inc': {'units_used': cost},
                        '$set': {'last_updated': datetime.now()}
                    },
                    upsert=True
                )
                
                logger.info(f"Recorded YouTube API call: {cost} units")
                return True
                
        except Exception as e:
            logger.error(f"Error recording API call: {e}")
            return False
    
    def get_cached_videos(self, recipe_name: str) -> Optional[List[Dict]]:
        """
        Get cached YouTube videos for a recipe.
        
        Args:
            recipe_name: Name of the recipe
        
        Returns:
            List of video dicts or None if not cached/expired
        """
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                cache_doc = db['youtube_cache'].find_one({
                    'recipe_name': recipe_name.lower()
                })
                
                if not cache_doc:
                    logger.debug(f"No cache found for: {recipe_name}")
                    return None
                
                # Check if cache is still valid (7 days)
                cached_at = cache_doc.get('cached_at')
                if not cached_at:
                    return None
                
                age = datetime.now() - cached_at
                if age > timedelta(days=self.cache_duration_days):
                    logger.info(f"Cache expired for: {recipe_name}")
                    return None
                
                videos = cache_doc.get('videos', [])
                logger.info(f"Cache hit for: {recipe_name} ({len(videos)} videos)")
                return videos
                
        except Exception as e:
            logger.error(f"Error retrieving cache: {e}")
            return None
    
    def cache_videos(self, recipe_name: str, videos: List[Dict]) -> bool:
        """Cache YouTube videos for a recipe."""
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                db['youtube_cache'].update_one(
                    {'recipe_name': recipe_name.lower()},
                    {
                        '$set': {
                            'videos': videos,
                            'cached_at': datetime.now()
                        }
                    },
                    upsert=True
                )
                return True
        except Exception as e:
            logger.error(f"Error caching videos: {e}")
            return False

    def record_and_cache(self, cost: int, recipe_name: str, videos: List[Dict]) -> bool:
        """Atomically record API call and cache results using a transaction."""
        try:
            client = self.database.get_client()
            txn_mgr = TransactionManager(client)
            
            def perform_ops(txn: MongoTransaction):
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                
                # 1. Record Quota
                txn.update_one(
                    "youtube_quota",
                    {'date': today},
                    {
                        '$inc': {'units_used': cost},
                        '$set': {'last_updated': datetime.now()}
                    },
                    upsert=True
                )
                
                # 2. Cache Videos
                txn.update_one(
                    "youtube_cache",
                    {'recipe_name': recipe_name.lower()},
                    {
                        '$set': {
                            'videos': videos,
                            'cached_at': datetime.now()
                        }
                    },
                    upsert=True
                )
            
            txn_mgr.execute_in_transaction(perform_ops)
            logger.info(f"Atomically recorded API call ({cost}) and cached {len(videos)} videos for: {recipe_name}")
            return True
        except Exception as e:
            logger.error(f"Error in record_and_cache transaction: {e}")
            return False
    
    def reset_quota(self) -> bool:
        """
        Reset quota counter (called at midnight UTC).
        
        Returns:
            True if reset successfully, False otherwise
        """
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                
                db['youtube_quota'].update_one(
                    {'date': today},
                    {
                        '$set': {
                            'units_used': 0,
                            'last_updated': datetime.now()
                        }
                    },
                    upsert=True
                )
                
                logger.info("YouTube quota reset for new day")
                return True
                
        except Exception as e:
            logger.error(f"Error resetting quota: {e}")
            return False


def fetch_youtube_videos_with_quota(
    youtube_service,
    recipe_name: str,
    quota_manager: YouTubeQuotaManager,
    max_results: int = 3
) -> List[Dict[str, str]]:
    """
    Fetch YouTube videos with quota management and caching.
    
    Strategy:
    1. Check cache first (free)
    2. If not cached, check remaining quota
    3. If quota sufficient, make API call and cache results
    4. If quota exceeded, return fallback generic search URL
    
    Args:
        youtube_service: YouTube API service object
        recipe_name: Name of the recipe
        quota_manager: QuotaManager instance
        max_results: Maximum number of videos to return (hard capped at 3)
    
    Returns:
        List of video dicts with 'title', 'url', 'thumbnail' keys
    """
    # HARD CAP: Never fetch more than 3 videos
    max_results = min(max_results, 3)
    
    # Step 1: Check cache
    cached_videos = quota_manager.get_cached_videos(recipe_name)
    if cached_videos:
        return cached_videos[:3]  # Enforce cap on cached results
    
    # Step 2: Check quota
    remaining_quota = quota_manager.get_remaining_quota()
    if remaining_quota < SEARCH_COST:
        logger.warning(f"YouTube quota exceeded ({remaining_quota} < {SEARCH_COST})")
        # Return fallback generic search URL
        return [{
            'title': f'Search YouTube for "{recipe_name}"',
            'url': f'https://www.youtube.com/results?search_query={recipe_name.replace(" ", "+")}+recipe+tutorial',
            'thumbnail': '',
            'fallback': True
        }]
    
    # Step 3: Make API call
    try:
        if not youtube_service:
            logger.warning("YouTube service not initialized")
            return []
        
        # Optimize search query for better YouTube results
        from src.services.integrations.youtube import optimize_youtube_query
        search_query = optimize_youtube_query(recipe_name)
        logger.debug(f"YouTube search: '{search_query}' (from '{recipe_name}')")
        
        search_response = youtube_service.search().list(
            q=search_query,
            part='id,snippet',
            maxResults=max_results,
            type='video',
            videoDuration='medium',
            relevanceLanguage='en',
            order='relevance'
        ).execute()
        
        if not search_response.get('items'):
            return []
        
        videos = []
        # Enforce cap: only process first 3 items
        for item in search_response['items'][:3]:
            video_id = item['id']['videoId']
            title = item['snippet']['title']
            videos.append({
                'title': title,
                'url': f"https://www.youtube.com/watch?v={video_id}",
                'thumbnail': item['snippet']['thumbnails']['medium']['url']
            })
        
        # Atomically record API usage and cache results
        quota_manager.record_and_cache(SEARCH_COST, recipe_name, videos)
        
        return videos[:3]  # Final enforcement of cap
        
    except Exception as e:
        logger.error(f"YouTube API error: {e}")
        # Return fallback URL on error
        return [{
            'title': f'Search YouTube for "{recipe_name}"',
            'url': f'https://www.youtube.com/results?search_query={recipe_name.replace(" ", "+")}+recipe+tutorial',
            'thumbnail': '',
            'fallback': True,
            'error': str(e)
        }]