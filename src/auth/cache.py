"""
Profile-Aware Cache Manager
Fixes cache invalidation on profile changes (Fix for Issue 3)
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


class CacheMetadata:
    """Metadata for cached responses."""
    
    def __init__(self, cached_at: datetime, profile_version: str, 
                 mode: str, invalidated: bool = False):
        self.cached_at = cached_at
        self.profile_version = profile_version
        self.mode = mode
        self.invalidated = invalidated
    
    def to_dict(self) -> Dict:
        return {
            "cached_at": self.cached_at.isoformat(),
            "profile_version": self.profile_version,
            "mode": self.mode,
            "invalidated": self.invalidated
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'CacheMetadata':
        return cls(
            cached_at=datetime.fromisoformat(data['cached_at']),
            profile_version=data['profile_version'],
            mode=data['mode'],
            invalidated=data.get('invalidated', False)
        )


def generate_profile_hash(user_profile: Dict) -> str:
    """
    Generate hash from user profile for cache key.
    
    Includes:
    - allergies
    - diet_types
    - cultural_restrictions
    
    Args:
        user_profile: User profile dict
    
    Returns:
        SHA-256 hash string (first 16 chars)
    """
    # Extract relevant fields
    allergies = sorted(user_profile.get('allergies', []))
    diet_types = sorted(user_profile.get('diet_types', []))
    cultural_restrictions = sorted(user_profile.get('cultural_restrictions', []))
    
    # Create fingerprint
    fingerprint = {
        'allergies': allergies,
        'diet_types': diet_types,
        'cultural_restrictions': cultural_restrictions
    }
    
    # Generate hash
    fingerprint_str = json.dumps(fingerprint, sort_keys=True)
    hash_obj = hashlib.sha256(fingerprint_str.encode('utf-8'))
    
    return hash_obj.hexdigest()[:16]  # First 16 chars


class ProfileAwareCacheManager:
    """Manages caching with profile awareness."""
    
    def __init__(self, cache_dir: str, ttl_hours: int = 12):
        """
        Initialize cache manager.
        
        Args:
            cache_dir: Directory for cache files
            ttl_hours: Time-to-live in hours (default 12 for profile-dependent)
        """
        self.cache_dir = cache_dir
        self.ttl_hours = ttl_hours
        self.memory_cache = {}
        
        # Ensure cache directory exists
        os.makedirs(cache_dir, exist_ok=True)
    
    def get_cache_key(self, image_hash: str, mode: str, 
                     user_profile: Optional[Dict] = None) -> str:
        """
        Generate cache key with profile awareness.
        
        Args:
            image_hash: Hash of the image
            mode: Cache mode (e.g., 'items', 'recipes')
            user_profile: Optional user profile for profile-dependent caching
        
        Returns:
            Cache key string
        """
        if user_profile:
            profile_hash = generate_profile_hash(user_profile)
            return f"{image_hash}_{mode}_{profile_hash}"
        else:
            return f"{image_hash}_{mode}"
    
    def get_cached_response(self, image_hash: str, mode: str,
                           user_profile: Optional[Dict] = None) -> Optional[str]:
        """
        Retrieve cached response if valid.
        
        Args:
            image_hash: Hash of the image
            mode: Cache mode
            user_profile: Optional user profile
        
        Returns:
            Cached response or None
        """
        cache_key = self.get_cache_key(image_hash, mode, user_profile)
        
        # Check memory cache first
        if cache_key in self.memory_cache:
            logger.info(f"Memory cache hit: {cache_key}")
            return self.memory_cache[cache_key]
        
        # Check disk cache
        cache_file = os.path.join(self.cache_dir, f"{cache_key}.json")
        if not os.path.exists(cache_file):
            logger.debug(f"Cache miss: {cache_key}")
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Check if invalidated
            if data.get('invalidated', False):
                logger.info(f"Cache invalidated: {cache_key}")
                os.remove(cache_file)
                return None
            
            # Check TTL
            cached_at = datetime.fromisoformat(data['cached_at'])
            age = datetime.now() - cached_at
            if age > timedelta(hours=self.ttl_hours):
                logger.info(f"Cache expired: {cache_key} (age: {age})")
                os.remove(cache_file)
                return None
            
            # Check profile version if profile-dependent
            if user_profile:
                current_profile_hash = generate_profile_hash(user_profile)
                cached_profile_hash = data.get('profile_version', '')
                if current_profile_hash != cached_profile_hash:
                    logger.info(f"Profile changed, cache invalid: {cache_key}")
                    os.remove(cache_file)
                    return None
            
            # Valid cache
            logger.info(f"Disk cache hit: {cache_key}")
            response = data['response']
            self.memory_cache[cache_key] = response
            return response
        
        except Exception as e:
            logger.error(f"Error reading cache {cache_key}: {e}")
            return None
    
    def cache_response(self, image_hash: str, mode: str, response: str,
                      user_profile: Optional[Dict] = None) -> bool:
        """
        Cache a response with metadata.
        
        Args:
            image_hash: Hash of the image
            mode: Cache mode
            response: Response to cache
            user_profile: Optional user profile
        
        Returns:
            True if cached successfully
        """
        cache_key = self.get_cache_key(image_hash, mode, user_profile)
        
        try:
            # Update memory cache
            self.memory_cache[cache_key] = response
            
            # Update disk cache
            cache_file = os.path.join(self.cache_dir, f"{cache_key}.json")
            
            cache_data = {
                "cached_at": datetime.now().isoformat(),
                "response": response,
                "mode": mode,
                "invalidated": False
            }
            
            if user_profile:
                cache_data["profile_version"] = generate_profile_hash(user_profile)
            
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2)
            
            logger.info(f"Cached response: {cache_key}")
            return True
        
        except Exception as e:
            logger.error(f"Error caching response {cache_key}: {e}")
            return False
    
    def invalidate_cache_for_user(self, username: str) -> int:
        """
        Invalidate all cache entries for a user.
        
        This is useful when user profile changes significantly.
        
        Args:
            username: Username to invalidate cache for
        
        Returns:
            Number of cache entries invalidated
        """
        count = 0
        
        try:
            # Iterate through all cache files
            for filename in os.listdir(self.cache_dir):
                if not filename.endswith('.json'):
                    continue
                
                cache_file = os.path.join(self.cache_dir, filename)
                
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    # Mark as invalidated
                    data['invalidated'] = True
                    
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                    
                    count += 1
                except Exception as e:
                    logger.error(f"Error invalidating {filename}: {e}")
            
            # Clear memory cache
            self.memory_cache.clear()
            
            logger.info(f"Invalidated {count} cache entries for user {username}")
            return count
        
        except Exception as e:
            logger.error(f"Error invalidating cache for user {username}: {e}")
            return 0
    
    def get_cache_stats(self) -> Dict:
        """
        Get cache statistics.
        
        Returns:
            Dict with cache stats
        """
        try:
            cache_files = [f for f in os.listdir(self.cache_dir) if f.endswith('.json')]
            
            total_size = 0
            valid_count = 0
            expired_count = 0
            invalidated_count = 0
            
            for filename in cache_files:
                cache_file = os.path.join(self.cache_dir, filename)
                total_size += os.path.getsize(cache_file)
                
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    if data.get('invalidated', False):
                        invalidated_count += 1
                    else:
                        cached_at = datetime.fromisoformat(data['cached_at'])
                        age = datetime.now() - cached_at
                        if age > timedelta(hours=self.ttl_hours):
                            expired_count += 1
                        else:
                            valid_count += 1
                except:
                    pass
            
            return {
                "total_entries": len(cache_files),
                "valid_entries": valid_count,
                "expired_entries": expired_count,
                "invalidated_entries": invalidated_count,
                "total_size_bytes": total_size,
                "memory_cache_entries": len(self.memory_cache)
            }
        
        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {}
    
    def cleanup_expired(self) -> int:
        """
        Remove expired and invalidated cache entries.
        
        Returns:
            Number of entries removed
        """
        count = 0
        
        try:
            for filename in os.listdir(self.cache_dir):
                if not filename.endswith('.json'):
                    continue
                
                cache_file = os.path.join(self.cache_dir, filename)
                
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    should_remove = False
                    
                    # Check if invalidated
                    if data.get('invalidated', False):
                        should_remove = True
                    else:
                        # Check if expired
                        cached_at = datetime.fromisoformat(data['cached_at'])
                        age = datetime.now() - cached_at
                        if age > timedelta(hours=self.ttl_hours):
                            should_remove = True
                    
                    if should_remove:
                        os.remove(cache_file)
                        count += 1
                
                except Exception as e:
                    logger.error(f"Error processing {filename}: {e}")
            
            logger.info(f"Cleaned up {count} expired/invalidated cache entries")
            return count
        
        except Exception as e:
            logger.error(f"Error during cache cleanup: {e}")
            return 0
