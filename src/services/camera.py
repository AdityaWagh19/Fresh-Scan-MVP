"""
Camera Service with Retry Logic and Circuit Breaker
Fixes infinite recursion and implements exponential backoff
"""

import os
import socket
import time
import random
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
from enum import Enum
from src.database.connection import DatabaseStateMachine, DatabaseConnectionContext
from src.utils.helpers import log_error
from src.config.constants import DEFAULT_SERVER_URL, CACHE_DIR


class MaxRetriesExceededError(Exception):
    """Raised when maximum retry attempts are exceeded."""
    pass


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker pattern implementation.
    Opens after consecutive failures, prevents cascading failures.
    """
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of consecutive failures before opening
            timeout: Seconds to wait before trying again (half-open state)
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
    
    def call(self, func, *args, **kwargs):
        """
        Execute function with circuit breaker protection.
        
        Args:
            func: Function to execute
            *args, **kwargs: Function arguments
        
        Returns:
            Function result
        
        Raises:
            Exception: If circuit is open or function fails
        """
        if self.state == CircuitState.OPEN:
            # Check if timeout has passed
            if self.last_failure_time and \
               (datetime.now() - self.last_failure_time).seconds >= self.timeout:
                self.state = CircuitState.HALF_OPEN
                print(f"Circuit breaker: HALF_OPEN (testing recovery)")
            else:
                raise Exception("Circuit breaker is OPEN - service unavailable")
        
        try:
            result = func(*args, **kwargs)
            # Success - reset circuit
            if self.state == CircuitState.HALF_OPEN:
                print(f"Circuit breaker: CLOSED (service recovered)")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            return result
        
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                print(f"Circuit breaker: OPEN (too many failures)")
            
            raise e
    
    def reset(self):
        """Manually reset circuit breaker."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None


class CameraService:
    """Handles all camera-related operations with retry logic and circuit breaker."""
    
    def __init__(self, database: DatabaseStateMachine, server_url=None, cache_dir=CACHE_DIR):
        self.database = database
        self.server_url = server_url or self._get_stored_server_url() or DEFAULT_SERVER_URL
        self.connection_status = False
        self.last_check_time = None
        self.cache_dir = cache_dir
        
        # Circuit breaker for connection failures
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, timeout=60)
        
        # Server availability cache
        self.availability_cache = {
            'is_available': False,
            'cached_at': None,
            'cache_ttl': 60  # seconds
        }
        
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(os.path.join(cache_dir, "images"), exist_ok=True)
    
    def _get_stored_server_url(self) -> Optional[str]:
        """Retrieve stored server URL from MongoDB."""
        try:
            client = self.database.get_or_create_client()
            with DatabaseConnectionContext(client) as db:
                config = db['system_config'].find_one({"config_type": "server_url"})
                if config and 'public_url' in config:
                    return config['public_url']
        except Exception as e:
            log_error("fetching server URL", e)
        return None
    
    def update_server_url(self) -> None:
        """Update the Raspberry Pi URL."""
        print(f"\nCurrent Raspberry Pi URL: {self.server_url}")
        new_url = input("Enter new URL (e.g., http://172.16.1.200:5000): ")
        if new_url:
            self.server_url = new_url
            try:
                client = self.database.get_or_create_client()
                with DatabaseConnectionContext(client) as db:
                    db['system_config'].update_one(
                        {"config_type": "server_url"},
                        {"$set": {"public_url": new_url, "last_updated": datetime.now()}},
                        upsert=True
                    )
            except Exception as e:
                log_error("saving server URL", e)
            
            print(f"\nUpdated URL to {new_url}")
            # Reset circuit breaker and cache
            self.circuit_breaker.reset()
            self._invalidate_availability_cache()
            self.check_connection(force=True)
    
    def _invalidate_availability_cache(self):
        """Invalidate server availability cache."""
        self.availability_cache['cached_at'] = None
    
    def _is_availability_cached(self) -> bool:
        """Check if availability status is cached and valid."""
        if not self.availability_cache['cached_at']:
            return False
        
        age = (datetime.now() - self.availability_cache['cached_at']).seconds
        return age < self.availability_cache['cache_ttl']
    
    def check_connection(self, force=False) -> bool:
        """
        Test connectivity to the Raspberry Pi server.
        
        Args:
            force: Force check even if recently checked
        
        Returns:
            True if connected, False otherwise
        """
        # Check cache first
        if not force and self._is_availability_cached():
            return self.availability_cache['is_available']
        
        current_time = datetime.now()
        if not force and self.last_check_time and (current_time - self.last_check_time).seconds < 60:
            return self.connection_status
        
        try:
            print(f"\nTesting connection to {self.server_url}...")
            
            # Try HTTP health check first
            try:
                response = requests.get(f"{self.server_url}/test", timeout=3)
                if response.status_code == 200:
                    self.connection_status = True
                    self.last_check_time = current_time
                    self._cache_availability(True)
                    print("\nConnection to Raspberry Pi successful!")
                    return True
            except:
                pass
            
            # Fallback to socket check
            url_parts = self.server_url.split(":")
            if len(url_parts) >= 2:
                hostname = url_parts[1].strip("/")
                port = int(url_parts[2].split("/")[0]) if len(url_parts) > 2 else 80
                
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                result = s.connect_ex((hostname, port))
                s.close()
                
                self.connection_status = (result == 0)
                self.last_check_time = current_time
                self._cache_availability(self.connection_status)
                
                if self.connection_status:
                    print("\nPort is open and reachable!")
                else:
                    print(f"\nPort {port} is not open on {hostname}")
                
                return self.connection_status
            else:
                print("\nInvalid URL format")
                self.connection_status = False
                self.last_check_time = current_time
                self._cache_availability(False)
                return False
        
        except Exception as e:
            self.connection_status = False
            self.last_check_time = current_time
            self._cache_availability(False)
            log_error("camera connection check", e)
            return False
    
    def _cache_availability(self, is_available: bool):
        """Cache server availability status."""
        self.availability_cache['is_available'] = is_available
        self.availability_cache['cached_at'] = datetime.now()
    
    def capture_image(self, max_retries: int = 3) -> Optional[Dict]:
        """
        Request the Raspberry Pi to capture an image (iterative with retries).
        
        Args:
            max_retries: Maximum number of retry attempts
        
        Returns:
            Dict with image metadata or None
        
        Raises:
            MaxRetriesExceededError: If all retries fail
        """
        retry_count = 0
        base_delay = 1  # seconds
        
        # Iterative approach (Fix for Issue 1 - no recursion)
        while retry_count < max_retries:
            # Check connection
            if not self.check_connection():
                print("\nWould you like to update the Raspberry Pi URL? (y/n)")
                if input().lower().startswith('y'):
                    self.update_server_url()
                    # Don't count this as a retry
                    continue
                else:
                    retry_count += 1
                    if retry_count >= max_retries:
                        raise MaxRetriesExceededError(
                            f"Failed to connect after {max_retries} attempts"
                        )
                    
                    # Exponential backoff
                    delay = base_delay * (2 ** (retry_count - 1))
                    print(f"\nRetrying in {delay} seconds... (attempt {retry_count}/{max_retries})")
                    time.sleep(delay)
                    continue
            
            try:
                endpoint = "/capture"
                print(f"\nRequesting image capture from {self.server_url}{endpoint}...")
                
                headers = {
                    "Accept": "application/json",
                    "User-Agent": "SmartFridgeSystem/2.0"
                }
                
                # Progressive timeout (3s, 5s, 10s)
                timeout = min(3 + (retry_count * 2), 15)
                
                response = requests.get(
                    f"{self.server_url}{endpoint}",
                    timeout=timeout,
                    headers=headers
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if 'image_id' in result:
                        print(f"\nImage captured with ID: {result['image_id']}")
                        return result
                    else:
                        print("\nImage captured successfully!")
                        return result
                else:
                    print(f"\nFailed to request image capture. Status code: {response.status_code}")
                    print(f"Response: {response.text[:200]}...")
                    retry_count += 1
            
            except requests.exceptions.ConnectionError:
                print("\nConnection refused. Is the camera service running?")
                retry_count += 1
            except requests.exceptions.Timeout:
                print(f"\nConnection timed out (timeout={timeout}s).")
                retry_count += 1
            except Exception as e:
                log_error("image capture", e)
                retry_count += 1
            
            # Exponential backoff before retry
            if retry_count < max_retries:
                delay = base_delay * (2 ** (retry_count - 1))
                print(f"\nRetrying in {delay} seconds... (attempt {retry_count}/{max_retries})")
                time.sleep(delay)
        
        # All retries exhausted
        raise MaxRetriesExceededError(
            f"Failed to capture image after {max_retries} attempts"
        )
    
    def get_latest_image(self, max_retries: int = 3) -> Tuple[Optional[str], Dict]:
        """
        Retrieve the latest image with exponential backoff (Fix for Issue 2).
        
        Args:
            max_retries: Maximum number of retry attempts
        
        Returns:
            Tuple of (image_path, metadata)
            metadata = {success: bool, attempts: int, total_time: float}
        """
        start_time = time.time()
        base_delay = 1  # seconds
        max_delay = 30  # seconds cap
        
        metadata = {
            'success': False,
            'attempts': 0,
            'total_time': 0.0,
            'delays_used': []
        }
        
        for attempt in range(max_retries):
            metadata['attempts'] = attempt + 1
            
            if not self.check_connection():
                print(f"\nAttempt {attempt + 1}: Connection unavailable")
                if attempt < max_retries - 1:
                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = random.uniform(0, 1)
                    total_delay = delay + jitter
                    metadata['delays_used'].append(total_delay)
                    
                    print(f"Retrying in {total_delay:.2f}s (exponential backoff)...")
                    time.sleep(total_delay)
                continue
            
            try:
                print(f"\nAttempt {attempt + 1}: Retrieving latest image...")
                
                # Progressive timeout
                timeout = min(3 + (attempt * 2), 15)
                
                # Use circuit breaker
                def fetch_image():
                    return requests.get(
                        f"{self.server_url}/latest_image",
                        timeout=timeout,
                        stream=True
                    )
                
                response = self.circuit_breaker.call(fetch_image)
                
                if response.status_code == 200:
                    temp_filename = f"latest_img_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                    temp_path = os.path.join(self.cache_dir, "images", temp_filename)
                    
                    with open(temp_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    metadata['success'] = True
                    metadata['total_time'] = time.time() - start_time
                    
                    print(f"\nImage retrieved successfully in {metadata['total_time']:.2f}s")
                    return temp_path, metadata
                else:
                    print(f"\nFailed (Status: {response.status_code})")
            
            except Exception as e:
                print(f"\nError: {str(e)}")
                log_error(f"image retrieval attempt {attempt + 1}", e)
            
            # Exponential backoff with jitter before next retry
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt), max_delay)
                jitter = random.uniform(0, 1)
                total_delay = delay + jitter
                metadata['delays_used'].append(total_delay)
                
                print(f"Retrying in {total_delay:.2f}s (exponential backoff + jitter)...")
                time.sleep(total_delay)
        
        metadata['total_time'] = time.time() - start_time
        print(f"\nFailed to retrieve image after {max_retries} attempts ({metadata['total_time']:.2f}s total)")
        return None, metadata
    
    def preprocess_image(self, image_path: str) -> str:
        """Preprocess image for better AI analysis results."""
        try:
            from PIL import Image, ImageOps
            img = Image.open(image_path)
            
            # Resize and compress image
            max_size = 800
            quality = 85
            
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size))
            
            enhanced_img = ImageOps.autocontrast(img, cutoff=2)
            
            preprocessed_path = image_path.replace('.jpg', '_processed.jpg')
            
            # Save with optimized quality
            enhanced_img.save(preprocessed_path, quality=quality, optimize=True)
            
            return preprocessed_path
        except Exception as e:
            log_error("image preprocessing", e)
            print("\nImage preprocessing failed. Using original image.")
            return image_path
    
    def get_circuit_breaker_status(self) -> Dict:
        """Get circuit breaker status for monitoring."""
        return {
            'state': self.circuit_breaker.state.value,
            'failure_count': self.circuit_breaker.failure_count,
            'last_failure_time': self.circuit_breaker.last_failure_time.isoformat() 
                if self.circuit_breaker.last_failure_time else None
        }