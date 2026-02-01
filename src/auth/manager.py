"""
New Authentication Integration for Smart Fridge System.

This module provides the new OAuth 2.0 authentication integration
that wraps around the existing UserProfileManager.
"""

from typing import Optional, Dict, Any
from src.auth.cli import CLIAuthAdapter
from src.auth.config import AuthConfig
from src.database import DatabaseStateMachine


class NewAuthManager:
    """
    Manages new OAuth 2.0 authentication and bridges to legacy UserProfileManager.
    
    This class provides a transition layer between the new authentication system
    and the existing Smart Fridge application.
    """
    
    def __init__(self, database: DatabaseStateMachine, legacy_user_mgr):
        """
        Initialize new authentication manager.
        
        Args:
            database: Database state machine
            legacy_user_mgr: Existing UserProfileManager instance
        """
        self.database = database
        self.legacy_user_mgr = legacy_user_mgr
        self.cli_auth = CLIAuthAdapter(database)
        self.current_session = None
        
        # Check if new auth is configured
        self.is_configured = AuthConfig.is_configured()
    
    def show_auth_menu(self) -> bool:
        """
        Show authentication menu and handle login/register.
        
        Returns:
            True if user authenticated successfully, False otherwise
        """
        if not self.is_configured:
            print("\n" + "="*60)
            print("AUTHENTICATION ERROR")
            print("="*60)
            print("\nThe authentication system is not configured.")
            print("Please run: python setup_auth.py")
            print("="*60)
            return False
        
        while True:
            print("\n" + "="*60)
            print("SMART FRIDGE - AUTHENTICATION")
            print("="*60)
            print("\nSecure Authentication System")
            print("(Email + Google OAuth Support)")
            print("\n" + "-"*60)
            print("\n1. Login with Email/Password")
            
            if AuthConfig.ENABLE_GOOGLE_OAUTH and AuthConfig.GOOGLE_CLIENT_ID:
                print("2. Login with Google")
            else:
                print("2. [Not Configured] Login with Google")
            
            print("3. Register New Account (Email/Password)")
            print("4. Password Reset")
            print("\n" + "-"*60)
            print("0. Exit")
            print("="*60)
            
            choice = input("\nEnter your choice: ").strip()
            
            if choice == '0':
                return False
            
            elif choice == '1':
                # Email/Password Login
                session = self.cli_auth.login_email_password()
                if session:
                    self.current_session = session
                    self._initialize_user_session(session)
                    return True
            
            elif choice == '2':
                # Google OAuth Login
                if not (AuthConfig.ENABLE_GOOGLE_OAUTH and AuthConfig.GOOGLE_CLIENT_ID):
                    print("\nGoogle OAuth is not configured.")
                    print("Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env")
                    input("\nPress Enter to continue...")
                    continue
                
                session = self.cli_auth.login_google_oauth()
                if session:
                    self.current_session = session
                    self._initialize_user_session(session)
                    return True
            
            elif choice == '3':
                # Register
                print("\n" + "="*60)
                print("NEW ACCOUNT REGISTRATION")
                print("="*60)
                print("\nPlease provide your household information:")
                
                # Collect minimal profile for registration
                profile = self._collect_registration_profile()
                
                session = self.cli_auth.register_email_password(profile)
                if session:
                    self.current_session = session
                    self._initialize_user_session(session)
                    return True
            
            elif choice == '4':
                # Password Reset
                self.cli_auth.request_password_reset()
                print("\nAfter resetting your password, please login again.")
                input("\nPress Enter to continue...")
            
            else:
                print("\nInvalid choice. Please try again.")
    
    def _collect_registration_profile(self) -> Dict[str, Any]:
        """
        Collect minimal profile information for registration.
        
        Returns:
            Profile dictionary
        """
        from src.config.constants import CUISINE_OPTIONS
        from src.utils.helpers import get_multiple_choice, input_with_default
        
        print("\nHousehold Information:")
        household_size = int(input_with_default(
            "Number of people in household (1-10)",
            "1"
        ))
        
        print("\nCuisine Preferences:")
        cuisines = get_multiple_choice(
            "Preferred cuisines",
            CUISINE_OPTIONS
        )
        
        return {
            'household_size': household_size,
            'preferred_cuisines': cuisines or [],
            'diet_types': [],
            'allergies': [],
            'age_groups': [],
            'cooking_frequency': 'Mixed',
            'shopping_frequency': 'Weekly',
            'cultural_restrictions': [],
            'cuisine_preferences': cuisines or [],
            'meal_frequency': 3,
            'preferred_proteins': [],
            'budget': 'medium'
        }
    
    def _initialize_user_session(self, session: Dict[str, Any]) -> None:
        """
        Initialize user session after successful login.
        
        Args:
            session: Authentication session from new system
        """
        from src.database import DatabaseConnectionContext
        from bson import ObjectId
        
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                # Get user from users_v2 collection
                user = db['users_v2'].find_one({'_id': ObjectId(session['user_id'])})
                
                if user:
                    # Map to a consistent format for the application
                    formatted_user = {
                        '_id': user['_id'],
                        'username': user['email'],  # User email as username
                        'email': user['email'],
                        'profile': user.get('profile', {}),
                        'inventory': user.get('inventory', []),
                        'grocery_lists': user.get('grocery_lists', []),
                        'favorite_recipes': user.get('favorite_recipes', [])
                    }
                    
                    # Set in legacy-aware user manager (still used for session state)
                    self.legacy_user_mgr.current_user = formatted_user
                    self.legacy_user_mgr.current_profile = user.get('profile', {})
                    
                    print(f"\nWelcome, {user['email']}!")

                    # Check for mandatory onboarding
                    if not user.get('is_onboarded', False):
                        self.legacy_user_mgr.run_onboarding_flow()
                else:
                    print("\nUser profile not found. Please contact support.")
                    
        except Exception as e:
            from src.utils.helpers import log_error
            log_error("initializing user session", e)
            print(f"\n Warning: Could not initialize user session: {e}")
    
    def logout(self) -> None:
        """Logout from auth system."""
        if self.current_session:
            self.cli_auth.logout()
            self.current_session = None
            self.legacy_user_mgr.current_user = None
            self.legacy_user_mgr.current_profile = None
