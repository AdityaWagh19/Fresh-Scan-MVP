"""
Smart Fridge System - Main Application
Fixes critical error handling and cleanup issues
"""

import logging
import sys
import os
import argparse

# Fix Unicode encoding for Windows console
if sys.platform == 'win32':
    # Set UTF-8 encoding for stdout and stderr
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    # Also set environment variable for subprocess
    os.environ['PYTHONIOENCODING'] = 'utf-8'
from typing import Dict, Optional
from src.database import (
    DatabaseStateMachine, DatabaseConnectionContext,
    ConnectionStatus, DatabaseConnectionError
)
from src.auth import UserProfileManager
from src.services import CameraService, MaxRetriesExceededError, VisionService
from src.services import InventoryManager
from src.services import RecipeManager
from src.utils.context import UserContextManager
from src.utils.helpers import log_error
from src.config.constants import MONGO_URI, CACHE_DIR
import pymongo


class CriticalError(Exception):
    """Raised for unrecoverable errors that require system shutdown."""
    pass


class ServiceHealth:
    """Tracks health status of system services."""
    
    def __init__(self):
        self.database = "unknown"
        self.camera = "unknown"
        self.ai = "unknown"
        self.integration = "unknown"
    
    def to_dict(self) -> Dict[str, str]:
        """Export health status as dict."""
        return {
            "database": self.database,
            "camera": self.camera,
            "ai": self.ai,
            "integration": self.integration
        }
    
    def is_critical_failure(self) -> bool:
        """Check if any critical service has failed."""
        return self.database == "error" or self.ai == "error"


class SmartFridgeSystem:
    """Main system class that orchestrates all components."""
    
    def __init__(self, debug: bool = False):
        """
        Initialize Smart Fridge System.
        
        Args:
            debug: Enable debug logging
        """
        self.debug = debug
        self.health = ServiceHealth()
        self.consecutive_db_failures = 0
        self.max_db_failures = 3
        
        # Initialize services
        self._setup_logging(debug)
        self._setup_database()
        self._setup_services()
    
    def _setup_logging(self, debug: bool) -> None:
        """Configure logging based on debug flag."""
        level = logging.DEBUG if debug else logging.INFO
        logging.basicConfig(
            filename='smart_fridge.log',
            level=level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        if debug:
            # Also log to console in debug mode
            console = logging.StreamHandler()
            console.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
            console.setFormatter(formatter)
            logging.getLogger('').addHandler(console)
    
    def _setup_database(self) -> None:
        """Configure database connection with SSL/TLS support."""
        try:
            # Configure database connection factory
            def create_connection():
                import certifi
                ca = certifi.where()
                
                return pymongo.MongoClient(
                    MONGO_URI,
                    tlsCAFile=ca,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=30000,
                    serverSelectionTimeoutMS=10000,
                    retryWrites=True,
                    retryReads=True,
                    tlsAllowInvalidCertificates=False
                )
            
            self.database = DatabaseStateMachine(create_connection)
            self.database.ensure_connected(max_retries=3)
            self.health.database = "ok"
            
        except DatabaseConnectionError as e:
            self.health.database = "error"
            logging.error(f"Database connection failed: {e}")
            raise CriticalError(f"Failed to connect to database: {e}")
        except Exception as e:
            self.health.database = "error"
            log_error("database setup", e)
            raise CriticalError(f"Database setup failed: {e}")
    
    def _setup_services(self) -> None:
        """Initialize all application services."""
        try:
            # Core services
            self.vision_service = VisionService()
            self.health.ai = "ok"
            
            self.user_mgr = UserProfileManager(self.database)
            self.camera_service = CameraService(self.database)
            self.inventory_mgr = InventoryManager(
                self.database, self.vision_service, self.user_mgr
            )
            
            # Context manager
            self.context_mgr = UserContextManager(self.user_mgr, self.inventory_mgr)
            
            # Recipe manager
            self.recipe_mgr = RecipeManager(
                self.database, self.inventory_mgr,
                self.vision_service, self.user_mgr, self.context_mgr
            )
            
            # Integration components (optional)
            self._setup_integration()
            
        except Exception as e:
            log_error("service setup", e)
            raise CriticalError(f"Failed to initialize services: {e}")
    
    def _setup_integration(self) -> None:
        """Setup optional integration services."""
        try:
            import os
            sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            
            from src.services.integrations.blinkit import get_order_automation, get_inventory_sync
            self.inventory_sync = get_inventory_sync()
            self.order_automation = get_order_automation(
                self.inventory_mgr, self.user_mgr, self.recipe_mgr
            )
            self.health.integration = "ok"
            logging.info("Blinkit integration loaded successfully")
            
        except ImportError as e:
            self.inventory_sync = None
            self.order_automation = None
            self.health.integration = "unavailable"
            self.integration_error = str(e)  # Store error for status display
            
            logging.warning(f"Blinkit integration unavailable: {e}")
            
            if self.debug:
                print(f"\n⚠️  Blinkit integration not available: {e}")
                print("   Running without Blinkit features")
                print("   Menu options 12-13 will be disabled\n")
    
    def check_system_health(self) -> Dict[str, str]:
        """
        Check health of all system services.
        
        Returns:
            Dict with service health status
        """
        # Check database
        try:
            if self.database.is_connected:
                self.health.database = "ok"
                self.consecutive_db_failures = 0
            else:
                self.health.database = "disconnected"
                self.consecutive_db_failures += 1
        except Exception:
            self.health.database = "error"
            self.consecutive_db_failures += 1
        
        # Check camera
        try:
            if self.camera_service.check_connection():
                self.health.camera = "ok"
            else:
                self.health.camera = "disconnected"
        except Exception:
            self.health.camera = "error"
        
        # AI service is always available (local)
        self.health.ai = "ok"
        
        return self.health.to_dict()
    
    def display_system_status(self) -> None:
        """Display system health status to user."""
        print("\n" + "="*50)
        print("SYSTEM STATUS")
        print("="*50)
        
        health = self.check_system_health()
        
        status_icons = {
            "ok": "",
            "disconnected": " ",
            "error": "",
            "unavailable": "-",
            "unknown": ""
        }
        
        for service, status in health.items():
            icon = status_icons.get(status, "")
            print(f"{icon} {service.capitalize()}: {status}")
        
        print("="*50)
        
        # Show integration details if unavailable
        if self.health.integration == "unavailable":
            print("\nIntegration Details:")
            if hasattr(self, 'integration_error'):
                print(f"   Error: {self.integration_error}")
            print("   Affected features:")
            print("   - Option 12: Order from Saved Grocery List")
            print("   - Option 13: Blinkit Integration & Ordering")
        
        # Warn if critical services are down
        if self.health.is_critical_failure():
            print("\n⚠️  WARNING: Critical services are unavailable!")
            print("Some features may not work properly.")
    
    def run(self) -> None:
        """Main application loop with hierarchical menu system."""
        try:
            # Startup health check
            if not self.debug:
                print("\nProgress: Initializing Smart Fridge System...")
            else:
                print("\n Performing startup health check...")
            
            health = self.check_system_health()
            
            if self.health.is_critical_failure():
                raise CriticalError("Critical services unavailable at startup")
            
            # System ready!
            if not self.debug:
                print("OK: System Ready!")
            else:
                print("System ready!")
            
            # Start Authentication
            from src.auth.manager import NewAuthManager
            new_auth = NewAuthManager(self.database, self.user_mgr)
            
            # User login
            if not new_auth.show_auth_menu():
                print("\nShutdown: No user authenticated.")
                return
            
            # Initialize hierarchical menu system
            from src.ui.menu import MenuSystem
            menu_system = MenuSystem(self)
            
            # Main menu loop with new system
            while True:
                # Health check before showing menu
                if self.consecutive_db_failures >= self.max_db_failures:
                    raise CriticalError(
                        f"Database unavailable for {self.max_db_failures} consecutive checks"
                    )
                
                try:
                    # Run menu system - returns False if user wants to logout
                    should_continue = menu_system.run()
                    
                    if not should_continue:
                        # User chose to logout
                        print("\n" + "="*60)
                        print("Thank you for using Smart Fridge System!")
                        user_identifier = self.user_mgr.current_user.get('email') or self.user_mgr.current_user.get('username', 'User')
                        print(f"Goodbye, {user_identifier}!")
                        print("="*60)
                        
                        # Logout from new auth if applicable
                        if hasattr(self, 'new_auth') and new_auth.current_session:
                            new_auth.logout()
                        
                        break
                
                except KeyboardInterrupt:
                    print("\n\n" + "="*60)
                    print("Program interrupted by user (Ctrl+C)")
                    print("Exiting Smart Fridge System...")
                    print("="*60)
                    break
                
                except DatabaseConnectionError as e:
                    # Critical error - database connection lost
                    logging.error(f"Database connection error: {e}")
                    self.consecutive_db_failures += 1
                    
                    if self.consecutive_db_failures >= self.max_db_failures:
                        raise CriticalError(
                            f"Database connection lost: {e}"
                        )
                    else:
                        print(f"\n⚠️ Database connection issue: {e}")
                        print(f"Attempts remaining: {self.max_db_failures - self.consecutive_db_failures}")
                        print("Trying to reconnect...")
                        
                        try:
                            self.database.ensure_connected(max_retries=2)
                            print("OK: Reconnected successfully!")
                            self.consecutive_db_failures = 0
                        except:
                            print("ERROR: Reconnection failed. Please try again.")
                
                except CriticalError:
                    # Re-raise critical errors
                    raise
                
                except Exception as e:
                    print(f"\nERROR: An error occurred: {e}")
                    log_error("menu operation", e)
                    print("Please try again or contact support if the issue persists.\n")
        
        except CriticalError as e:
            print(f"\nERROR: CRITICAL ERROR: {e}")
            print("The system cannot continue and will shut down.")
            logging.critical(f"Critical error: {e}", exc_info=True)
        
        except Exception as e:
            log_error("main application", e)
            print(f"\nERROR: Unexpected error: {e}")
            logging.critical(f"Unexpected error: {e}", exc_info=True)
        
        finally:
            # Safe cleanup
            self._safe_cleanup()
    
    def _execute_menu_choice(self, choice: str) -> None:
        """Execute menu choice with error handling."""
        if choice == '1':
            self.inventory_mgr.scan_fridge(self.camera_service)
        elif choice == '2':
            self.inventory_mgr.display_inventory()
        elif choice == '3':
            self.inventory_mgr.add_item_manually()
        elif choice == '4':
            self.inventory_mgr.edit_item()
        elif choice == '5':
            self.inventory_mgr.remove_item()
        elif choice == '6':
            self.recipe_mgr.suggest_recipes()
        elif choice == '7':
            self.recipe_mgr.view_favorite_recipes()
        elif choice == '8':
            self.inventory_mgr.generate_grocery_list()
        elif choice == '9':
            self.inventory_mgr.view_grocery_lists()
        elif choice == '10':
            self.user_mgr.view_profile()
        elif choice == '11':
            self.user_mgr.edit_profile()
        elif choice == '12':
            # Check integration availability before executing
            if self.health.integration != "ok":
                print("\nERROR: Blinkit integration is not available")
                print("   Reason: Integration module failed to load")
                print("   Please check that the integration/ folder exists")
                return
            self.inventory_mgr.order_from_saved_list()
        elif choice == '13':
            # Check integration availability before executing
            if self.health.integration != "ok":
                print("\nERROR: Blinkit integration is not available")
                print("   Reason: Integration module failed to load")
                print("   Please check that the integration/ folder exists")
                return
            self._run_integration_menu()
    
    def _run_integration_menu(self) -> None:
        """Run integration menu with error handling."""
        try:
            if self.order_automation is None or self.inventory_sync is None:
                print("Integration features not available.")
                print("Check if integration/ folder exists.")
                return
            
            # CRITICAL: Get current username for session isolation
            if not self.user_mgr.current_user:
                print("Please log into Smart Fridge first.")
                return
            
            username = self.user_mgr.current_user['username']
            
            import asyncio
            from src.services.integrations.blinkit import run_integration_menu
            asyncio.run(run_integration_menu(
                self.order_automation, self.inventory_sync, username=username
            ))
        except ImportError as e:
            print(f"Integration module not available: {e}")
        except Exception as e:
            print(f"Error in integration menu: {e}")
            log_error("integration menu", e)
    
    def _safe_cleanup(self) -> None:
        """
        Safely cleanup resources (Fix for Issue 2).
        
        Ensures cleanup exceptions don't mask original errors.
        """
        cleanup_errors = []
        
        # Cleanup database (Fix for Issue 2 - use unified disconnect)
        if hasattr(self, 'database') and self.database:
            try:
                print("\n Closing database connection...")
                self.database.disconnect()
                print("Database connection closed.")
            except Exception as e:
                cleanup_errors.append(f"Database disconnect: {e}")
                log_error("database cleanup", e)
        
        # Cleanup integration and Blinkit sessions in a single loop
        async def run_async_cleanup():
            tasks = []
            
            # 1. Cleanup order automation
            if hasattr(self, 'order_automation') and self.order_automation:
                try:
                    print("Cleaning up integration services...")
                    tasks.append(self.order_automation.close())
                except Exception as e:
                    cleanup_errors.append(f"Integration automation cleanup setup: {e}")

            # 2. Cleanup per-user Blinkit sessions
            try:
                from src.services.integrations.blinkit import get_active_blinkit_users, clear_user_blinkit_service
                active_users = get_active_blinkit_users()
                if active_users:
                    print(f"Cleaning up Blinkit sessions for {len(active_users)} active users...")
                    for user in active_users:
                        tasks.append(clear_user_blinkit_service(user))
            except Exception as e:
                cleanup_errors.append(f"Blinkit session cleanup setup: {e}")

            if tasks:
                # Return exceptions True so one fail doesn't stop others
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, res in enumerate(results):
                    if isinstance(res, Exception):
                        cleanup_errors.append(f"Async cleanup task {i} failed: {res}")
                
                # Tiny sleep to allow transports to finish closing
                await asyncio.sleep(0.5)

        import asyncio
        try:
            asyncio.run(run_async_cleanup())
            print("Integration services and sessions closed.")
        except Exception as e:
            cleanup_errors.append(f"Unified async cleanup failed: {e}")
        
        # Report cleanup errors (but don't raise)
        if cleanup_errors:
            print("\n  Some cleanup operations failed:")
            for error in cleanup_errors:
                print(f"- {error}")
    
    def _display_main_menu(self) -> None:
        """Display the main menu with dynamic integration options."""
        print("\n" + "="*50)
        print("SMART FRIDGE SYSTEM")
        print("="*50)
        print("1.   Scan Fridge Contents")
        print("2.   View Current Inventory")
        print("3.  + Add Item Manually")
        print("4.    Edit Inventory Item")
        print("5.  - Remove Item from Inventory")
        print("6.   Get Recipe Suggestions")
        print("7.   View Favorite Recipes")
        print("8.   Generate/Manage Grocery List")
        print("9.   View Saved Grocery Lists")
        print("10.  View User Profile")
        print("11.   Edit User Profile")
        
        # Dynamic Blinkit options based on availability
        if self.health.integration == "ok":
            print("12.  Order from Saved Grocery List (Blinkit)")
            print("13.  Blinkit Integration & Ordering")
        else:
            print("12.  [Unavailable] Order from Saved Grocery List")
            print("13.  [Unavailable] Blinkit Integration")
        
        print("14.  Check System Status")
        print("0.   Exit")
        print("="*50)
        
        # Show integration status if unavailable
        if self.health.integration != "ok":
            print("\nINFO: Blinkit integration is currently unavailable")
            print("   Check System Status (option 14) for details")


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(description='Smart Fridge System')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug logging')
    args = parser.parse_args()
    
    try:
        app = SmartFridgeSystem(debug=args.debug)
        app.run()
    except KeyboardInterrupt:
        print("\n\n Program terminated by user. Goodbye!")
    except CriticalError as e:
        print(f"\n System shutdown due to critical error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n Fatal error: {e}")
        logging.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        print("\n Smart Fridge System has been shut down.")


if __name__ == "__main__":
    main()
