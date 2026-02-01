"""
Thread-Safe Database Connection Manager
Fixes double-checked locking and adds explicit connection methods
"""

import enum
import threading
import time
import pymongo
from typing import Optional, Callable, Dict
from datetime import datetime, timedelta
from src.config.constants import MONGO_URI
from src.utils.helpers import log_error


class ConnectionStatus(enum.Enum):
    """Enum representing different database connection states."""
    DISCONNECTED = enum.auto()
    CONNECTING = enum.auto()
    CONNECTED = enum.auto()
    ERROR = enum.auto()


class DatabaseConnectionError(Exception):
    """Custom exception for database connection failures."""
    pass


class ConnectionMetrics:
    """Tracks connection metrics for monitoring."""
    
    def __init__(self):
        self.connection_attempts = 0
        self.connection_failures = 0
        self.total_connection_time = 0.0
        self.last_connection_time = None
        self.last_error = None
    
    @property
    def avg_connection_time(self) -> float:
        """Average connection time in seconds."""
        if self.connection_attempts == 0:
            return 0.0
        return self.total_connection_time / self.connection_attempts
    
    @property
    def success_rate(self) -> float:
        """Connection success rate (0.0-1.0)."""
        if self.connection_attempts == 0:
            return 0.0
        successes = self.connection_attempts - self.connection_failures
        return successes / self.connection_attempts
    
    def to_dict(self) -> Dict:
        """Export metrics as dict."""
        return {
            "connection_attempts": self.connection_attempts,
            "connection_failures": self.connection_failures,
            "avg_connection_time": self.avg_connection_time,
            "success_rate": self.success_rate,
            "last_connection_time": self.last_connection_time.isoformat() if self.last_connection_time else None,
            "last_error": self.last_error
        }


class DatabaseConnectionContext:
    """Context manager for database connections."""
    
    def __init__(self, client: pymongo.MongoClient, db_name: str = "SmartKitchen"):
        self._client = client
        self._db_name = db_name
        self._connection = None

    def __enter__(self):
        self._connection = self._client[self._db_name]
        return self._connection

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class DatabaseStateMachine:
    """
    Thread-safe state machine for managing database connections.
    
    Fixes:
    - Issue 1: Removed unsafe double-checked locking, uses RLock
    - Issue 2: Explicit method names (get_or_create_client, ensure_connected)
    
    Features:
    - Connection health checks
    - Metrics tracking
    - Retry logic
    - Connection string validation
    """
    
    def __init__(self, connection_factory: Callable[[], pymongo.MongoClient],
                 health_check_interval: int = 30):
        """
        Initialize database state machine.
        
        Args:
            connection_factory: Factory function to create MongoClient
            health_check_interval: Seconds between health checks (default: 30)
        """
        self._factory = connection_factory
        self._client: Optional[pymongo.MongoClient] = None
        self._state = ConnectionStatus.DISCONNECTED
        
        # Use RLock for reentrant locking (Fix for Issue 1)
        self._lock = threading.RLock()
        
        # State change event for signaling
        self._state_changed = threading.Event()
        
        self._connection_error: Optional[str] = None
        self._metrics = ConnectionMetrics()
        
        # Health check configuration
        self._health_check_interval = health_check_interval
        self._last_health_check = None
        self._health_check_thread = None
        self._shutdown = threading.Event()
    
    def ensure_connected(self, max_retries: int = 3) -> None:
        """
        Ensure database connection is established.
        
        This is an explicit method for connecting. Unlike get_client(),
        this method's purpose is clear from its name.
        
        Args:
            max_retries: Maximum number of connection attempts
        
        Raises:
            DatabaseConnectionError: If connection fails after retries
        """
        # Always acquire lock first (Fix for Issue 1 - no unsafe outer check)
        with self._lock:
            # If already connected, return immediately
            if self._state == ConnectionStatus.CONNECTED:
                return
            
            # Try to connect with retries
            for attempt in range(max_retries):
                try:
                    self._metrics.connection_attempts += 1
                    start_time = time.time()
                    
                    # Set state to CONNECTING
                    self._state = ConnectionStatus.CONNECTING
                    self._state_changed.set()
                    
                    # Create client
                    self._client = self._factory()
                    
                    # Validate connection
                    self._client.server_info()
                    
                    # Record metrics
                    connection_time = time.time() - start_time
                    self._metrics.total_connection_time += connection_time
                    self._metrics.last_connection_time = datetime.now()
                    
                    # Update state
                    self._state = ConnectionStatus.CONNECTED
                    self._connection_error = None
                    self._state_changed.set()
                    
                    # Start health check thread
                    self._start_health_check()
                    
                    return
                
                except Exception as e:
                    self._metrics.connection_failures += 1
                    self._metrics.last_error = str(e)
                    
                    self._state = ConnectionStatus.ERROR
                    self._client = None
                    self._connection_error = str(e)
                    self._state_changed.set()
                    
                    # If not last attempt, wait before retry
                    if attempt < max_retries - 1:
                        time.sleep(1 * (attempt + 1))  # Exponential backoff
                    else:
                        # Last attempt failed
                        raise DatabaseConnectionError(
                            f"Failed to establish database connection after {max_retries} attempts: {e}"
                        )
    
    def get_or_create_client(self, max_retries: int = 3) -> pymongo.MongoClient:
        """
        Get MongoDB client, connecting if necessary.
        
        This method may raise exceptions if connection fails.
        Use get_client_if_connected() for a non-throwing version.
        
        Args:
            max_retries: Maximum connection attempts if not connected
        
        Returns:
            pymongo.MongoClient instance
        
        Raises:
            DatabaseConnectionError: If connection fails
        
        Note:
            This method auto-connects, which may be unexpected.
            Consider using ensure_connected() explicitly.
        """
        with self._lock:
            if self._state != ConnectionStatus.CONNECTED:
                self.ensure_connected(max_retries)
            
            if self._state == ConnectionStatus.ERROR:
                raise DatabaseConnectionError(
                    f"Cannot establish database connection. Last error: {self._connection_error}"
                )
            
            return self._client
    
    def get_client(self, max_retries: int = 3) -> pymongo.MongoClient:
        """
        Legacy alias for get_or_create_client for backward compatibility.
        """
        return self.get_or_create_client(max_retries)
    
    def get_client_if_connected(self) -> Optional[pymongo.MongoClient]:
        """
        Get MongoDB client only if already connected.
        
        This method never raises exceptions and never auto-connects.
        
        Returns:
            pymongo.MongoClient if connected, None otherwise
        """
        with self._lock:
            if self._state == ConnectionStatus.CONNECTED:
                return self._client
            return None
    
    def disconnect(self) -> None:
        """
        Disconnect from database and clean up resources.
        """
        # Set shutdown flag outside of lock to allow loop to exit if it's waiting for lock
        self._shutdown.set()
        
        with self._lock:
            # Join health check thread if it's running
            if self._health_check_thread and self._health_check_thread.is_alive():
                # We don't join with long timeout here if we're inside the thread itself
                if threading.current_thread() != self._health_check_thread:
                    self._health_check_thread.join(timeout=2)
            
            # Close client
            if self._client:
                try:
                    self._client.close()
                except:
                    pass
                self._client = None
            
            # Update state
            self._state = ConnectionStatus.DISCONNECTED
            self._connection_error = None
            self._state_changed.set()
    
    def _start_health_check(self) -> None:
        """Start background health check thread."""
        with self._lock:
            if self._health_check_thread and self._health_check_thread.is_alive():
                return
            
            self._shutdown.clear()
            self._health_check_thread = threading.Thread(
                target=self._health_check_loop,
                daemon=True,
                name="DatabaseHealthCheck"
            )
            self._health_check_thread.start()
    
    def _health_check_loop(self) -> None:
        """Background loop for health checks."""
        while not self._shutdown.is_set():
            # Wait for interval or shutdown signal
            # Using Event.wait(timeout) is better than time.sleep(timeout)
            # because it can be interrupted immediately
            if self._shutdown.wait(timeout=self._health_check_interval):
                break
            
            # Perform health check
            try:
                with self._lock:
                    # Final check for shutdown inside the lock
                    if self._shutdown.is_set():
                        break
                        
                    if self._state == ConnectionStatus.CONNECTED and self._client:
                        try:
                            # Ping database
                            self._client.admin.command('ping')
                            self._last_health_check = datetime.now()
                        except pymongo.errors.InvalidOperation:
                            # Client was closed externally
                            if not self._shutdown.is_set():
                                log_error("database health check", "MongoClient closed unexpectedly")
                            break
                        except Exception as e:
                            # Connection lost
                            if not self._shutdown.is_set():
                                log_error("database health check", e)
                                self._state = ConnectionStatus.ERROR
                                self._connection_error = f"Health check failed: {e}"
                                self._state_changed.set()
            except Exception as e:
                if not self._shutdown.is_set():
                    log_error("health check loop error", e)
                break
    
    def get_metrics(self) -> Dict:
        """
        Get connection metrics.
        
        Returns:
            Dict with connection statistics
        """
        with self._lock:
            metrics = self._metrics.to_dict()
            metrics['current_state'] = self._state.name
            metrics['last_health_check'] = self._last_health_check.isoformat() if self._last_health_check else None
            return metrics
    
    @property
    def status(self) -> ConnectionStatus:
        """Get current connection status."""
        with self._lock:
            return self._state
    
    @property
    def error(self) -> Optional[str]:
        """Get last connection error."""
        with self._lock:
            return self._connection_error
    
    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        with self._lock:
            return self._state == ConnectionStatus.CONNECTED
    
    def __del__(self):
        """Cleanup on deletion."""
        try:
            self.disconnect()
        except:
            pass


# Backward compatibility aliases
def connect(state_machine: DatabaseStateMachine) -> None:
    """
    Legacy connect function for backward compatibility.
    
    Deprecated: Use ensure_connected() instead.
    """
    state_machine.ensure_connected()


def get_client(state_machine: DatabaseStateMachine) -> pymongo.MongoClient:
    """
    Legacy get_client function for backward compatibility.
    
    Deprecated: Use get_or_create_client() instead.
    """
    return state_machine.get_or_create_client()