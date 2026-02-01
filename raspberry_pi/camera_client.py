"""
Camera Client for Smart Fridge Application
Provides interface to communicate with Raspberry Pi Camera Service via Cloudflare Tunnel
"""

import os
import time
import requests
from typing import Optional, Dict, Any, Union
from dotenv import load_dotenv
import logging
from pathlib import Path

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CameraClient:
    """
    Client for interacting with Raspberry Pi Camera Service
    
    Features:
    - Automatic retry with exponential backoff
    - Comprehensive error handling
    - Health monitoring
    - Image capture and retrieval
    - Session management
    
    Usage:
        camera = CameraClient()
        if camera.is_healthy():
            image_path = camera.capture_and_retrieve()
    """
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 30,
        max_retries: int = 3
    ):
        """
        Initialize Camera Client
        
        Args:
            base_url: Base URL of camera service (defaults to CLOUDFLARE_DOMAIN from .env)
            api_key: API key for authentication (defaults to CAMERA_API_KEY from .env)
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
        """
        # Load configuration
        domain = base_url or os.getenv('CLOUDFLARE_DOMAIN')
        if not domain or domain == 'None':
            raise ValueError(
                "CLOUDFLARE_DOMAIN not configured. "
                "Set CLOUDFLARE_DOMAIN in .env file or pass base_url parameter"
            )
        
        # Ensure HTTPS
        if not domain.startswith('http'):
            domain = f"https://{domain}"
        
        self.base_url = domain
        self.api_key = api_key or os.getenv('CAMERA_API_KEY')
        self.timeout = timeout
        self.max_retries = max_retries
        
        # Statistics
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_duration_ms': 0
        }
        
        logger.info(f"Camera client initialized: {self.base_url}")
    
    def _make_request(
        self,
        endpoint: str,
        method: str = 'GET',
        retry_count: int = 0,
        **kwargs
    ) -> Optional[requests.Response]:
        """
        Make HTTP request with retry logic
        
        Args:
            endpoint: API endpoint (e.g., '/test')
            method: HTTP method (GET, POST, etc.)
            retry_count: Current retry attempt
            **kwargs: Additional arguments for requests
        
        Returns:
            Response object or None if failed
        """
        url = f"{self.base_url}{endpoint}"
        headers = kwargs.pop('headers', {})
        
        # Add API key if configured
        if self.api_key:
            headers['X-API-Key'] = self.api_key
        
        start_time = time.time()
        
        try:
            logger.debug(f"Request: {method} {url}")
            
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                timeout=self.timeout,
                verify=True,  # Verify SSL certificate
                **kwargs
            )
            
            duration_ms = int((time.time() - start_time) * 1000)
            
            response.raise_for_status()
            
            # Update statistics
            self.stats['total_requests'] += 1
            self.stats['successful_requests'] += 1
            self.stats['total_duration_ms'] += duration_ms
            
            logger.debug(f"Request successful ({duration_ms}ms)")
            return response
        
        except requests.exceptions.Timeout:
            logger.warning(f"Request timeout for {endpoint}")
            
            if retry_count < self.max_retries:
                wait_time = 2 ** retry_count  # Exponential backoff
                logger.info(
                    f"Retrying in {wait_time}s... "
                    f"(attempt {retry_count + 1}/{self.max_retries})"
                )
                time.sleep(wait_time)
                return self._make_request(endpoint, method, retry_count + 1, **kwargs)
            
            logger.error(f"Max retries exceeded for {endpoint}")
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            return None
        
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error: {e}")
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            return None
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            return None
    
    def test_connection(self) -> Optional[Dict[str, Any]]:
        """
        Test if camera service is reachable
        
        Returns:
            Dict with status information or None if failed
        """
        response = self._make_request('/test')
        if response:
            return response.json()
        return None
    
    def health_check(self) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive health status of camera service
        
        Returns:
            Dict with health information including:
            - status: overall status (healthy/degraded)
            - components: status of each component (database, camera, disk)
            - timestamp: current timestamp
        """
        response = self._make_request('/health')
        if response:
            return response.json()
        return None
    
    def is_healthy(self) -> bool:
        """
        Check if camera service is healthy
        
        Returns:
            True if service is healthy, False otherwise
        """
        health = self.health_check()
        if not health:
            return False
        return health.get('status') == 'healthy'
    
    def capture_image(self) -> Optional[Dict[str, Any]]:
        """
        Trigger image capture on Raspberry Pi
        
        Returns:
            Dict with capture information:
            - status: success/error
            - message: status message
            - image_path: path on Raspberry Pi
            - image_id: MongoDB document ID
            - timestamp: capture timestamp
        """
        response = self._make_request('/capture')
        if response:
            return response.json()
        return None
    
    def get_latest_image(
        self,
        save_path: Optional[Union[str, Path]] = None,
        as_bytes: bool = False
    ) -> Optional[Union[str, bytes]]:
        """
        Retrieve the most recently captured image
        
        Args:
            save_path: Path to save image (if None, returns bytes)
            as_bytes: If True, return image as bytes instead of saving
        
        Returns:
            Path to saved image, bytes, or None if failed
        """
        response = self._make_request('/latest_image')
        if not response:
            return None
        
        if as_bytes:
            return response.content
        
        # Save to file
        if not save_path:
            save_path = f"fridge_image_{int(time.time())}.jpg"
        
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(save_path, 'wb') as f:
            f.write(response.content)
        
        logger.info(f"Image saved to: {save_path}")
        return str(save_path)
    
    def list_images(self) -> Optional[Dict[str, Any]]:
        """
        Get metadata for all captured images
        
        Returns:
            Dict with:
            - status: success/error
            - total_images: count of images
            - images: list of image metadata
        """
        response = self._make_request('/images')
        if response:
            return response.json()
        return None
    
    def get_image_by_id(
        self,
        image_id: str,
        save_path: Optional[Union[str, Path]] = None,
        as_bytes: bool = False
    ) -> Optional[Union[str, bytes]]:
        """
        Retrieve specific image by MongoDB ID
        
        Args:
            image_id: MongoDB document ID
            save_path: Path to save image
            as_bytes: If True, return image as bytes
        
        Returns:
            Path to saved image, bytes, or None if failed
        """
        response = self._make_request(f'/image/{image_id}')
        if not response:
            return None
        
        if as_bytes:
            return response.content
        
        if not save_path:
            save_path = f"image_{image_id}.jpg"
        
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(save_path, 'wb') as f:
            f.write(response.content)
        
        logger.info(f"Image saved to: {save_path}")
        return str(save_path)
    
    def capture_and_retrieve(
        self,
        save_path: Optional[Union[str, Path]] = None,
        wait_time: float = 1.0
    ) -> Optional[str]:
        """
        Capture new image and immediately retrieve it
        
        This is a convenience method that combines capture and retrieval
        
        Args:
            save_path: Path to save captured image
            wait_time: Seconds to wait between capture and retrieval
        
        Returns:
            Path to saved image or None if failed
        """
        # Step 1: Trigger capture
        logger.info("Capturing image...")
        capture_result = self.capture_image()
        
        if not capture_result:
            logger.error("Failed to capture image")
            return None
        
        if capture_result.get('status') != 'success':
            logger.error(f"Capture failed: {capture_result.get('message')}")
            return None
        
        image_id = capture_result.get('image_id')
        logger.info(f"Image captured with ID: {image_id}")
        
        # Step 2: Wait briefly for image to be ready
        time.sleep(wait_time)
        
        # Step 3: Retrieve the image
        logger.info("Retrieving image...")
        image_path = self.get_latest_image(save_path=save_path)
        
        if image_path:
            logger.info(f"Image captured and saved: {image_path}")
        else:
            logger.error("Failed to retrieve captured image")
        
        return image_path
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get client usage statistics
        
        Returns:
            Dict with statistics:
            - total_requests: total number of requests
            - successful_requests: number of successful requests
            - failed_requests: number of failed requests
            - success_rate: percentage of successful requests
            - avg_duration_ms: average request duration
        """
        total = self.stats['total_requests']
        
        return {
            'total_requests': total,
            'successful_requests': self.stats['successful_requests'],
            'failed_requests': self.stats['failed_requests'],
            'success_rate': (
                self.stats['successful_requests'] / total * 100
                if total > 0 else 0
            ),
            'avg_duration_ms': (
                self.stats['total_duration_ms'] / total
                if total > 0 else 0
            )
        }


# Example usage
if __name__ == "__main__":
    # Initialize client
    camera = CameraClient()
    
    print("=" * 60)
    print("Camera Client Test")
    print("=" * 60)
    
    # Test 1: Connection
    print("\n1. Testing connection...")
    test_result = camera.test_connection()
    if test_result:
        print(f"   ✓ {test_result.get('message')}")
    else:
        print("   ✗ Connection failed")
    
    # Test 2: Health check
    print("\n2. Checking health...")
    health = camera.health_check()
    if health:
        print(f"   Status: {health.get('status')}")
        print(f"   Components: {health.get('components')}")
    else:
        print("   ✗ Health check failed")
    
    # Test 3: Capture and retrieve
    print("\n3. Capturing image...")
    image_path = camera.capture_and_retrieve(save_path='test_capture.jpg')
    if image_path:
        print(f"   ✓ Image saved: {image_path}")
    else:
        print("   ✗ Capture failed")
    
    # Test 4: List images
    print("\n4. Listing images...")
    images = camera.list_images()
    if images:
        print(f"   Total images: {images.get('total_images')}")
    else:
        print("   ✗ Failed to list images")
    
    # Statistics
    print("\n" + "=" * 60)
    print("Statistics:")
    stats = camera.get_statistics()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    print("=" * 60)
