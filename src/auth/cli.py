"""
CLI Authentication Adapter.

Provides CLI-specific authentication flows including:
- Email/password login
- OAuth Device Authorization Flow (for future OAuth support)
- Secure token storage in local file
"""

import os
import json
import getpass
import logging
from typing import Optional, Dict, Any
from pathlib import Path

from src.auth.providers import AuthCredentials
from src.auth.oauth import get_token_manager, TokenType
from src.auth.oauth import get_auth_service
from src.auth.oauth import PKCESession
from src.database import DatabaseStateMachine
from src.utils.helpers import log_error

logger = logging.getLogger(__name__)


class CLIAuthAdapter:
    """
    CLI authentication adapter supporting:
    - Email/password login
    - Secure token storage in local file
    - Session management
    """
    
    def __init__(self, database: DatabaseStateMachine):
        """
        Initialize CLI auth adapter.
        
        Args:
            database: Database connection
        """
        self.database = database
        self.auth_service = get_auth_service(database)
        self.token_manager = get_token_manager()
        self.token_file = Path.home() / '.smart_fridge' / 'auth_token'
    
    def login_email_password(self) -> Optional[Dict[str, Any]]:
        """
        Email/password login flow for CLI.
        
        Returns:
            Session info dict or None if failed
        """
        print("\n" + "="*50)
        print("EMAIL LOGIN")
        print("="*50)
        
        email = input("Email: ").strip()
        password = getpass.getpass("Password: ")
        
        # Create credentials
        credentials = AuthCredentials(
            provider='email',
            data={
                'email': email,
                'password': password,
                'interface': 'cli',
                'ip_address': 'localhost'
            }
        )
        
        # Authenticate
        result, tokens = self.auth_service.authenticate_user('email', credentials)
        
        if not result.success:
            print(f"\nERROR: Login failed: {result.error_message}")
            return None
        
        if result.requires_verification:
            print("\nWARNING:  Email verification required")
            print("Please check your email for verification link")
            return None
        
        # Save tokens
        self._save_tokens(tokens.to_dict())
        
        print(f"\nOK: Welcome back, {email}!")
        
        return {
            'user_id': result.user_id,
            'email': result.email,
            'tokens': tokens.to_dict()
        }
    
    def register_email_password(self, profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Email/password registration flow for CLI.
        
        Args:
            profile: User profile data
            
        Returns:
            Session info dict or None if failed
        """
        print("\n" + "="*50)
        print("NEW USER REGISTRATION")
        print("="*50)
        
        email = input("Email: ").strip()
        password = getpass.getpass("Choose a password: ")
        confirm_password = getpass.getpass("Confirm password: ")
        
        if password != confirm_password:
            print("\nERROR: Passwords don't match")
            return None
        
        # Create credentials
        credentials = AuthCredentials(
            provider='email',
            data={
                'email': email,
                'password': password,
                'interface': 'cli',
                'ip_address': 'localhost'
            }
        )
        
        # Register
        result, tokens = self.auth_service.register_user('email', credentials, profile)
        
        if not result.success:
            print(f"\nERROR: Registration failed: {result.error_message}")
            return None
        
        if result.requires_verification:
            print("\nOK: Registration successful!")
            print("WARNING:  Please check your email to verify your account")
            return None
        
        # Save tokens
        if tokens:
            self._save_tokens(tokens.to_dict())
        
        print(f"\nOK: Registration successful! Welcome, {email}!")
        
        return {
            'user_id': result.user_id,
            'email': result.email,
            'tokens': tokens.to_dict() if tokens else None
        }
    
    def logout(self) -> None:
        """Logout and clear saved tokens."""
        try:
            # Get current tokens
            tokens = self._load_tokens()
            if tokens and 'access_token' in tokens:
                # Revoke session
                self.auth_service.logout(tokens['access_token'])
            
            # Clear token file
            if self.token_file.exists():
                self.token_file.unlink()
            
            print("\nOK: Logged out successfully")
            
        except Exception as e:
            log_error("CLI logout", e)
            print("\nWARNING:  Logout completed (with errors)")
    
    def get_current_session(self) -> Optional[Dict[str, Any]]:
        """
        Get current session from saved tokens.
        
        Returns:
            Session info or None if no valid session
        """
        try:
            tokens = self._load_tokens()
            if not tokens or 'access_token' not in tokens:
                return None
            
            # Validate access token
            session = self.auth_service.validate_session(tokens['access_token'])
            if session:
                session['tokens'] = tokens
                return session
            
            # Try to refresh token
            if 'refresh_token' in tokens:
                new_tokens = self.auth_service.refresh_token(tokens['refresh_token'])
                if new_tokens:
                    self._save_tokens(new_tokens.to_dict())
                    session = self.auth_service.validate_session(new_tokens.access_token)
                    if session:
                        session['tokens'] = new_tokens.to_dict()
                        return session
            
            # No valid session
            return None
            
        except Exception as e:
            log_error("get current session", e)
            return None
    
    def _save_tokens(self, tokens: Dict[str, Any]) -> None:
        """
        Save tokens to local file.
        
        Args:
            tokens: Token dictionary
        """
        try:
            # Create directory if needed
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Write tokens
            with open(self.token_file, 'w') as f:
                json.dump(tokens, f)
            
            # Set file permissions (owner read/write only)
            if os.name != 'nt':  # Unix-like systems
                os.chmod(self.token_file, 0o600)
            
            logger.info("Tokens saved successfully")
            
        except Exception as e:
            log_error("save tokens", e)
    
    def _load_tokens(self) -> Optional[Dict[str, Any]]:
        """
        Load tokens from local file.
        
        Returns:
            Token dictionary or None
        """
        try:
            if not self.token_file.exists():
                return None
            
            with open(self.token_file, 'r') as f:
                return json.load(f)
                
        except Exception as e:
            log_error("load tokens", e)
            return None
    
    def login_google_oauth(self) -> Optional[Dict[str, Any]]:
        """
        Google OAuth login flow for CLI.
        
        Returns:
            Session info dict or None if failed
        """
        print("\n" + "="*50)
        print("GOOGLE OAUTH LOGIN")
        print("="*50)
        print("[DEBUG] Starting Google OAuth login flow...")
        
        # Check if Google OAuth is configured
        from src.auth.providers import GoogleOAuthProvider
        from src.auth.oauth import LocalOAuthCallbackServer, open_browser_for_oauth
        
        print("[DEBUG] Initializing Google OAuth provider...")
        google_provider = GoogleOAuthProvider(self.database)
        
        if not google_provider.is_configured():
            print("\nERROR: Google OAuth is not configured")
            print("Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env")
            return None
        
        print("[DEBUG] Generating PKCE session...")
        # Generate PKCE session
        pkce_session = PKCESession()
        
        # Save PKCE session temporarily
        self._save_pkce_session(pkce_session)
        
        print("[DEBUG] Generating authorization URL...")
        # Generate authorization URL
        auth_url = google_provider.generate_authorization_url(pkce_session)
        
        print("\n🌐 Opening browser for Google authentication...")
        print("If browser doesn't open, visit this URL:")
        print(f"\n{auth_url}\n")
        
        print("[DEBUG] Creating callback server on port 3000...")
        # Start local callback server
        callback_server = LocalOAuthCallbackServer(port=3000)
        
        try:
            print("[DEBUG] Starting callback server...")
            callback_server.start()
            print("[DEBUG] Callback server started successfully")
            
            print("[DEBUG] Opening browser...")
            # Open browser
            if not open_browser_for_oauth(auth_url):
                print("WARNING:  Failed to open browser automatically")
                print("Please open the URL above manually")
            
            print("\n⏳ Waiting for authentication...")
            print("(This will timeout in 5 minutes)")
            
            print("[DEBUG] Waiting for OAuth callback...")
            # Wait for callback
            callback_data = callback_server.wait_for_callback(timeout=300)
            
            print(f"[DEBUG] Callback received: {callback_data is not None}")
            
            if not callback_data:
                print("\nERROR: Authentication timeout")
                return None
            
            if 'error' in callback_data:
                print(f"\nERROR: Authentication failed: {callback_data.get('error_description', 'Unknown error')}")
                return None
            
            print("[DEBUG] Verifying state parameter...")
            # Verify state parameter (CSRF protection)
            if callback_data.get('state') != pkce_session.state:
                print("\nERROR: Invalid state parameter - possible CSRF attack")
                return None
            
            print("[DEBUG] Creating authentication credentials...")
            # Create credentials for OAuth authentication
            credentials = AuthCredentials(
                provider='google',
                data={
                    'authorization_code': callback_data['code'],
                    'code_verifier': pkce_session.code_verifier,
                    'state': callback_data['state'],
                    'interface': 'cli',
                    'ip_address': 'localhost'
                }
            )
            
            print("[DEBUG] Authenticating with Google provider...")
            # Authenticate with Google
            result, tokens = self.auth_service.authenticate_user('google', credentials)
            
            print(f"[DEBUG] Authentication result: success={result.success}")
            
            if not result.success:
                print(f"\nERROR: Authentication failed: {result.error_message}")
                return None
            
            print("[DEBUG] Saving tokens to local storage...")
            # Save tokens
            self._save_tokens(tokens.to_dict())
            print("[DEBUG] Tokens saved successfully")
            
            print(f"\nOK: Welcome, {result.email}!")
            
            print("[DEBUG] Creating session dictionary...")
            session_dict = {
                'user_id': result.user_id,
                'email': result.email,
                'tokens': tokens.to_dict()
            }
            
            print("[DEBUG] Returning session to caller...")
            return session_dict
            
        finally:
            print("[DEBUG] Entering finally block...")
            print("[DEBUG] Stopping callback server...")
            callback_server.stop()
            print("[DEBUG] Callback server stopped")
            
            print("[DEBUG] Clearing PKCE session...")
            self._clear_pkce_session()
            print("[DEBUG] PKCE session cleared")
            print("[DEBUG] OAuth login method complete")
    
    def _save_pkce_session(self, pkce_session: PKCESession) -> None:
        """Save PKCE session temporarily."""
        try:
            import json
            from pathlib import Path
            
            pkce_file = Path.home() / '.smart_fridge' / 'pkce_session'
            pkce_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(pkce_file, 'w') as f:
                json.dump(pkce_session.to_dict(), f)
                
        except Exception as e:
            log_error("save PKCE session", e)
    
    def _load_pkce_session(self) -> Optional[PKCESession]:
        """Load PKCE session."""
        try:
            import json
            from pathlib import Path
            from src.auth.oauth import PKCESession
            
            pkce_file = Path.home() / '.smart_fridge' / 'pkce_session'
            
            if not pkce_file.exists():
                return None
            
            with open(pkce_file, 'r') as f:
                data = json.load(f)
                return PKCESession.from_dict(data)
                
        except Exception as e:
            log_error("load PKCE session", e)
            return None
    
    def _clear_pkce_session(self) -> None:
        """Clear PKCE session file."""
        try:
            from pathlib import Path
            
            pkce_file = Path.home() / '.smart_fridge' / 'pkce_session'
            
            if pkce_file.exists():
                pkce_file.unlink()
                
        except Exception as e:
            log_error("clear PKCE session", e)
    
    def request_password_reset(self) -> None:
        """Request password reset flow."""
        print("\n" + "="*50)
        print("PASSWORD RESET REQUEST")
        print("="*50)
        
        email = input("Email: ").strip()
        
        # Get email provider
        provider = self.auth_service.get_provider('email')
        if not provider or not provider.supports_password_reset():
            print("\nERROR: Password reset not supported")
            return
        
        # Request reset
        success, reset_token, error = provider.request_password_reset(email)
        
        if not success:
            print(f"\nERROR: Failed to request password reset: {error}")
            return
        
        print("\nOK: Password reset requested")
        print("\nIn production, a reset link would be sent to your email.")
        print("For development, here's your reset token:")
        print(f"\n{reset_token}\n")
        print("Use this token with the 'Reset Password' option")
    
    def reset_password_with_token(self) -> None:
        """Reset password using token."""
        print("\n" + "="*50)
        print("RESET PASSWORD")
        print("="*50)
        
        reset_token = input("Reset token: ").strip()
        new_password = getpass.getpass("New password: ")
        confirm_password = getpass.getpass("Confirm new password: ")
        
        if new_password != confirm_password:
            print("\nERROR: Passwords don't match")
            return
        
        # Get email provider
        provider = self.auth_service.get_provider('email')
        if not provider:
            print("\nERROR: Email provider not available")
            return
        
        # Reset password
        success, error = provider.reset_password(reset_token, new_password)
        
        if not success:
            print(f"\nERROR: Password reset failed: {error}")
            return
        
        print("\nOK: Password reset successful!")
        print("Please login with your new password")
