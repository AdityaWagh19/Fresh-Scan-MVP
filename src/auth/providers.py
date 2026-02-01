"""
Merged module from:
  - src/auth/providers/email_password_provider.py
  - src/auth/providers/google_oauth_provider.py
  - src/auth/core/auth_provider.py
"""

# Imports
from abc import ABC, abstractmethod
from bcrypt import hashpw, gensalt, checkpw
from bson import ObjectId
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, TYPE_CHECKING
from urllib.parse import urlencode
import logging
import requests

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from src.database import DatabaseStateMachine, DatabaseConnectionContext

# ===== From src/auth/providers/email_password_provider.py =====

from src.auth.oauth import get_token_manager, TokenType
from src.auth.validators import validate_email, normalize_email
from src.auth.config import AuthConfig
from src.auth.validators import PasswordValidator, validate_password_with_feedback
from src.utils.helpers import log_error

logger = logging.getLogger(__name__)

# ===== From src/auth/core/auth_provider.py =====

@dataclass
class AuthCredentials:
    """Authentication credentials (provider-specific)."""
    provider: str
    data: Dict[str, Any]


@dataclass
class UserInfo:
    """User information from authentication provider."""
    email: str
    email_verified: bool
    provider: str
    provider_user_id: str
    profile: Dict[str, Any]


@dataclass
class AuthResult:
    """Result of authentication attempt."""
    success: bool
    user_id: Optional[str] = None
    email: Optional[str] = None
    error_message: Optional[str] = None
    requires_verification: bool = False
    metadata: Optional[Dict[str, Any]] = None


class AuthProvider(ABC):
    """
    Base class for authentication providers.
    
    All authentication providers (Email/Password, Google OAuth, etc.)
    must implement this interface.
    """
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Get provider name (e.g., 'email', 'google')."""
        pass
    
    @abstractmethod
    def authenticate(self, credentials: AuthCredentials) -> AuthResult:
        """
        Authenticate user with provider-specific credentials.
        
        Args:
            credentials: Authentication credentials
            
        Returns:
            AuthResult with success status and user info
        """
        pass
    
    @abstractmethod
    def register(self, credentials: AuthCredentials, profile: Dict[str, Any]) -> AuthResult:
        """
        Register new user with this provider.
        
        Args:
            credentials: Registration credentials
            profile: User profile data
            
        Returns:
            AuthResult with success status and user info
        """
        pass
    
    def supports_password_reset(self) -> bool:
        """Check if provider supports password reset."""
        return False
    
    def supports_email_verification(self) -> bool:
        """Check if provider supports email verification."""
        return False


class EmailPasswordProvider(AuthProvider):
    """
    Email and password authentication provider.
    
    Security features:
    - RFC 5322 email validation
    - bcrypt hashing (cost factor ≥12)
    - Password strength enforcement (entropy, complexity)
    - Account lockout after 5 failed attempts
    - Secure password reset with JWT tokens
    """
    
    def __init__(self, database: 'DatabaseStateMachine'):
        """
        Initialize email/password provider.
        
        Args:
            database: Database connection
        """
        self.database = database
        self.token_manager = get_token_manager()
    
    @property
    def provider_name(self) -> str:
        """Get provider name."""
        return "email"
    
    def supports_password_reset(self) -> bool:
        """Email provider supports password reset."""
        return True
    
    def supports_email_verification(self) -> bool:
        """Email provider supports email verification."""
        return AuthConfig.REQUIRE_EMAIL_VERIFICATION
    
    def register(self, credentials: AuthCredentials, profile: Dict[str, Any]) -> AuthResult:
        """
        Register new user with email and password.
        
        Args:
            credentials: Must contain 'email' and 'password'
            profile: User profile data
            
        Returns:
            AuthResult with success status
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            email = credentials.data.get('email', '').strip()
            password = credentials.data.get('password', '')
            
            # Validate email
            email_valid, email_error = validate_email(email)
            if not email_valid:
                return AuthResult(
                    success=False,
                    error_message=f"Invalid email: {email_error}"
                )
            
            # Normalize email
            email = normalize_email(email)
            
            # Validate password
            password_valid, password_feedback = validate_password_with_feedback(password, email)
            if not password_valid:
                return AuthResult(
                    success=False,
                    error_message=f"Invalid password: {password_feedback}"
                )
            
            # Check if email already exists
            with DatabaseConnectionContext(self.database.get_client()) as db:
                existing_user = db['users_v2'].find_one({'email': email})
                if existing_user:
                    return AuthResult(
                        success=False,
                        error_message="Email already registered"
                    )
                
                # Hash password
                password_hash = hashpw(
                    password.encode('utf-8'),
                    gensalt(rounds=AuthConfig.BCRYPT_COST_FACTOR)
                )
                
                # Create user document
                user_doc = {
                    'email': email,
                    'email_verified': not AuthConfig.REQUIRE_EMAIL_VERIFICATION,
                    'auth_provider': 'email',
                    'password_hash': password_hash,
                    'oauth_accounts': [],
                    'profile': profile,
                    'security': {
                        'failed_login_attempts': 0,
                        'locked_until': None,
                        'last_login': None,
                        'last_password_change': datetime.utcnow(),
                        'password_reset_token': None,
                        'password_reset_expires': None
                    },
                    'created_at': datetime.utcnow(),
                    'updated_at': datetime.utcnow()
                }
                
                # Insert user
                result = db['users_v2'].insert_one(user_doc)
                user_id = str(result.inserted_id)
                
                # Log registration
                db['auth_audit_log'].insert_one({
                    'event_type': 'user_registered',
                    'user_id': result.inserted_id,
                    'email': email,
                    'provider': 'email',
                    'success': True,
                    'timestamp': datetime.utcnow()
                })
                
                logger.info(f"User registered successfully: {email}")
                
                return AuthResult(
                    success=True,
                    user_id=user_id,
                    email=email,
                    requires_verification=AuthConfig.REQUIRE_EMAIL_VERIFICATION
                )
                
        except Exception as e:
            log_error("email registration", e)
            return AuthResult(
                success=False,
                error_message="Registration failed due to server error"
            )
    
    def authenticate(self, credentials: AuthCredentials) -> AuthResult:
        """
        Authenticate user with email and password.
        
        Args:
            credentials: Must contain 'email' and 'password'
            
        Returns:
            AuthResult with success status
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            email = normalize_email(credentials.data.get('email', ''))
            password = credentials.data.get('password', '')
            ip_address = credentials.data.get('ip_address', 'unknown')
            
            with DatabaseConnectionContext(self.database.get_client()) as db:
                # Find user
                user = db['users_v2'].find_one({'email': email, 'auth_provider': 'email'})
                
                if not user:
                    # Log failed attempt
                    db['auth_audit_log'].insert_one({
                        'event_type': 'login_failed',
                        'user_id': None,
                        'email': email,
                        'provider': 'email',
                        'ip_address': ip_address,
                        'success': False,
                        'failure_reason': 'user_not_found',
                        'timestamp': datetime.utcnow()
                    })
                    
                    return AuthResult(
                        success=False,
                        error_message="Invalid email or password"
                    )
                
                # Check if account is locked
                locked_until = user['security'].get('locked_until')
                if locked_until and datetime.utcnow() < locked_until:
                    remaining = (locked_until - datetime.utcnow()).seconds // 60
                    return AuthResult(
                        success=False,
                        error_message=f"Account locked. Try again in {remaining} minutes"
                    )
                
                # Verify password
                if not checkpw(password.encode('utf-8'), user['password_hash']):
                    # Increment failed attempts
                    failed_attempts = user['security']['failed_login_attempts'] + 1
                    
                    update_data = {
                        'security.failed_login_attempts': failed_attempts
                    }
                    
                    # Lock account if max attempts reached
                    if failed_attempts >= AuthConfig.MAX_LOGIN_ATTEMPTS:
                        lockout_until = datetime.utcnow() + timedelta(seconds=AuthConfig.LOCKOUT_DURATION)
                        update_data['security.locked_until'] = lockout_until
                        logger.warning(f"Account locked due to failed attempts: {email}")
                    
                    db['users_v2'].update_one(
                        {'_id': user['_id']},
                        {'$set': update_data}
                    )
                    
                    # Log failed attempt
                    db['auth_audit_log'].insert_one({
                        'event_type': 'login_failed',
                        'user_id': user['_id'],
                        'email': email,
                        'provider': 'email',
                        'ip_address': ip_address,
                        'success': False,
                        'failure_reason': 'invalid_password',
                        'timestamp': datetime.utcnow()
                    })
                    
                    return AuthResult(
                        success=False,
                        error_message="Invalid email or password"
                    )
                
                # Check email verification if required
                if AuthConfig.REQUIRE_EMAIL_VERIFICATION and not user['email_verified']:
                    return AuthResult(
                        success=False,
                        error_message="Email not verified. Please check your email for verification link",
                        requires_verification=True
                    )
                
                # Successful login - reset failed attempts
                db['users_v2'].update_one(
                    {'_id': user['_id']},
                    {
                        '$set': {
                            'security.failed_login_attempts': 0,
                            'security.locked_until': None,
                            'security.last_login': datetime.utcnow()
                        }
                    }
                )
                
                # Log successful login
                db['auth_audit_log'].insert_one({
                    'event_type': 'login_success',
                    'user_id': user['_id'],
                    'email': email,
                    'provider': 'email',
                    'ip_address': ip_address,
                    'success': True,
                    'timestamp': datetime.utcnow()
                })
                
                logger.info(f"User logged in successfully: {email}")
                
                return AuthResult(
                    success=True,
                    user_id=str(user['_id']),
                    email=email
                )
                
        except Exception as e:
            log_error("email authentication", e)
            return AuthResult(
                success=False,
                error_message="Authentication failed due to server error"
            )
    
    def request_password_reset(self, email: str) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Request password reset token.
        
        Args:
            email: User email address
            
        Returns:
            Tuple of (success, reset_token, error_message)
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            email = normalize_email(email)
            
            with DatabaseConnectionContext(self.database.get_client()) as db:
                user = db['users_v2'].find_one({'email': email, 'auth_provider': 'email'})
                
                if not user:
                    # Don't reveal if email exists
                    return True, None, None
                
                # Generate reset token
                reset_token = self.token_manager.generate_reset_token(
                    str(user['_id']),
                    email
                )
                
                # Store token hash in database
                db['users_v2'].update_one(
                    {'_id': user['_id']},
                    {
                        '$set': {
                            'security.password_reset_token': reset_token,
                            'security.password_reset_expires': datetime.utcnow() + timedelta(seconds=AuthConfig.JWT_RESET_TOKEN_EXPIRY)
                        }
                    }
                )
                
                # Log password reset request
                db['auth_audit_log'].insert_one({
                    'event_type': 'password_reset_requested',
                    'user_id': user['_id'],
                    'email': email,
                    'provider': 'email',
                    'success': True,
                    'timestamp': datetime.utcnow()
                })
                
                logger.info(f"Password reset requested: {email}")
                
                return True, reset_token, None
                
        except Exception as e:
            log_error("password reset request", e)
            return False, None, "Failed to process password reset request"
    
    def reset_password(self, reset_token: str, new_password: str) -> tuple[bool, Optional[str]]:
        """
        Reset password using reset token.
        
        Args:
            reset_token: Password reset token
            new_password: New password
            
        Returns:
            Tuple of (success, error_message)
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            # Validate reset token
            claims = self.token_manager.validate_token(reset_token, TokenType.RESET)
            if not claims:
                return False, "Invalid or expired reset token"
            
            email = claims.email
            user_id = claims.sub
            
            # Validate new password
            password_valid, password_feedback = validate_password_with_feedback(new_password, email)
            if not password_valid:
                return False, f"Invalid password: {password_feedback}"
            
            with DatabaseConnectionContext(self.database.get_client()) as db:
                user = db['users_v2'].find_one({'_id': ObjectId(user_id)})
                
                if not user:
                    return False, "User not found"
                
                # Verify token matches stored token
                if user['security'].get('password_reset_token') != reset_token:
                    return False, "Invalid reset token"
                
                # Check token expiry
                expires = user['security'].get('password_reset_expires')
                if not expires or datetime.utcnow() > expires:
                    return False, "Reset token has expired"
                
                # Hash new password
                password_hash = hashpw(
                    new_password.encode('utf-8'),
                    gensalt(rounds=AuthConfig.BCRYPT_COST_FACTOR)
                )
                
                # Update password and clear reset token
                db['users_v2'].update_one(
                    {'_id': user['_id']},
                    {
                        '$set': {
                            'password_hash': password_hash,
                            'security.password_reset_token': None,
                            'security.password_reset_expires': None,
                            'security.last_password_change': datetime.utcnow(),
                            'security.failed_login_attempts': 0,
                            'security.locked_until': None
                        }
                    }
                )
                
                # Revoke all existing sessions (force re-login)
                db['auth_sessions'].update_many(
                    {'user_id': user['_id'], 'revoked': False},
                    {'$set': {'revoked': True}}
                )
                
                # Log password reset
                db['auth_audit_log'].insert_one({
                    'event_type': 'password_reset_completed',
                    'user_id': user['_id'],
                    'email': email,
                    'provider': 'email',
                    'success': True,
                    'timestamp': datetime.utcnow()
                })
                
                logger.info(f"Password reset completed: {email}")
                
                return True, None
                
        except Exception as e:
            log_error("password reset", e)
            return False, "Failed to reset password"

# ===== From src/auth/providers/google_oauth_provider.py =====

from src.auth.oauth import PKCESession
from src.auth.config import AuthConfig
from src.utils.helpers import log_error

logger = logging.getLogger(__name__)


class GoogleOAuthProvider(AuthProvider):
    """
    Google OAuth 2.0 authentication provider.
    
    Implements:
    - Authorization Code Flow with PKCE (RFC 7636)
    - OpenID Connect ID token validation
    - Just-in-time user provisioning
    - Account linking by email
    """
    
    # Google OAuth endpoints
    AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
    JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
    
    # OAuth scopes
    SCOPES = [
        "openid",
        "email",
        "profile"
    ]
    
    def __init__(self, database: 'DatabaseStateMachine'):
        """
        Initialize Google OAuth provider.
        
        Args:
            database: Database connection
        """
        self.database = database
        self.client_id = AuthConfig.GOOGLE_CLIENT_ID
        self.client_secret = AuthConfig.GOOGLE_CLIENT_SECRET
        self.redirect_uri = AuthConfig.GOOGLE_REDIRECT_URI
        
        if not self.client_id or not self.client_secret:
            logger.warning("Google OAuth not configured - missing client credentials")
    
    @property
    def provider_name(self) -> str:
        """Get provider name."""
        return "google"
    
    def is_configured(self) -> bool:
        """Check if provider is properly configured."""
        return bool(self.client_id and self.client_secret)
    
    def generate_authorization_url(self, pkce_session: PKCESession) -> str:
        """
        Generate Google OAuth authorization URL with PKCE.
        
        Args:
            pkce_session: PKCE session with code challenge and state
            
        Returns:
            Authorization URL
        """
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(self.SCOPES),
            'state': pkce_session.state,
            'code_challenge': pkce_session.code_challenge,
            'code_challenge_method': pkce_session.challenge_method,
            'access_type': 'offline',  # Request refresh token
            'prompt': 'consent'  # Force consent screen to get refresh token
        }
        
        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"
    
    def exchange_code_for_tokens(
        self,
        authorization_code: str,
        code_verifier: str
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Exchange authorization code for access and ID tokens.
        
        Args:
            authorization_code: Authorization code from callback
            code_verifier: PKCE code verifier
            
        Returns:
            Tuple of (success, token_response, error_message)
        """
        try:
            # Prepare token request
            data = {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'code': authorization_code,
                'code_verifier': code_verifier,
                'grant_type': 'authorization_code',
                'redirect_uri': self.redirect_uri
            }
            
            # Exchange code for tokens
            response = requests.post(
                self.TOKEN_ENDPOINT,
                data=data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            
            if response.status_code != 200:
                error_data = response.json()
                error_msg = error_data.get('error_description', 'Token exchange failed')
                logger.error(f"Google token exchange failed: {error_msg}")
                return False, None, error_msg
            
            tokens = response.json()
            
            # Validate required fields
            if 'access_token' not in tokens or 'id_token' not in tokens:
                return False, None, "Invalid token response from Google"
            
            return True, tokens, None
            
        except Exception as e:
            log_error("Google token exchange", e)
            return False, None, str(e)
    
    def validate_id_token(self, id_token: str) -> Tuple[bool, Optional[UserInfo], Optional[str]]:
        """
        Validate Google ID token and extract user info.
        
        Note: For production, should verify JWT signature using Google's public keys.
        For now, we'll decode and validate basic claims.
        
        Args:
            id_token: Google ID token (JWT)
            
        Returns:
            Tuple of (success, user_info, error_message)
        """
        try:
            import jwt
            
            # Decode without verification (for development)
            # In production, should verify signature with Google's public keys
            payload = jwt.decode(
                id_token,
                options={"verify_signature": False}
            )
            
            # Validate issuer
            if payload.get('iss') not in ['https://accounts.google.com', 'accounts.google.com']:
                return False, None, "Invalid token issuer"
            
            # Validate audience
            if payload.get('aud') != self.client_id:
                return False, None, "Invalid token audience"
            
            # Check expiry
            exp = payload.get('exp', 0)
            if datetime.utcnow().timestamp() > exp:
                return False, None, "Token has expired"
            
            # Extract user info
            email = payload.get('email')
            if not email:
                return False, None, "Email not provided by Google"
            
            user_info = UserInfo(
                email=normalize_email(email),
                email_verified=payload.get('email_verified', False),
                provider='google',
                provider_user_id=payload.get('sub'),
                profile={
                    'name': payload.get('name'),
                    'given_name': payload.get('given_name'),
                    'family_name': payload.get('family_name'),
                    'picture': payload.get('picture'),
                    'locale': payload.get('locale')
                }
            )
            
            return True, user_info, None
            
        except Exception as e:
            log_error("Google ID token validation", e)
            return False, None, str(e)
    
    def get_user_info(self, access_token: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Get user info from Google using access token.
        
        Args:
            access_token: Google access token
            
        Returns:
            Tuple of (success, user_info, error_message)
        """
        try:
            response = requests.get(
                self.USERINFO_ENDPOINT,
                headers={'Authorization': f'Bearer {access_token}'}
            )
            
            if response.status_code != 200:
                return False, None, "Failed to fetch user info from Google"
            
            return True, response.json(), None
            
        except Exception as e:
            log_error("Google user info fetch", e)
            return False, None, str(e)
    
    def provision_or_link_user(
        self,
        user_info: UserInfo,
        default_profile: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Provision new user or link OAuth account to existing user.
        
        Args:
            user_info: User information from Google
            default_profile: Default profile data for new users
            
        Returns:
            Tuple of (success, user_id, error_message)
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            with DatabaseConnectionContext(self.database.get_client()) as db:
                # Check if user exists with this email
                existing_user = db['users_v2'].find_one({'email': user_info.email})
                
                if existing_user:
                    # Link OAuth account to existing user
                    oauth_account = {
                        'provider': 'google',
                        'provider_user_id': user_info.provider_user_id,
                        'linked_at': datetime.utcnow(),
                        'profile': user_info.profile
                    }
                    
                    # Check if already linked
                    already_linked = any(
                        acc.get('provider') == 'google' and 
                        acc.get('provider_user_id') == user_info.provider_user_id
                        for acc in existing_user.get('oauth_accounts', [])
                    )
                    
                    if not already_linked:
                        db['users_v2'].update_one(
                            {'_id': existing_user['_id']},
                            {
                                '$push': {'oauth_accounts': oauth_account},
                                '$set': {'updated_at': datetime.utcnow()}
                            }
                        )
                        logger.info(f"Linked Google account to existing user: {user_info.email}")
                    
                    return True, str(existing_user['_id']), None
                
                else:
                    # Create new user
                    user_doc = {
                        'email': user_info.email,
                        'email_verified': user_info.email_verified,
                        'auth_provider': 'google',
                        'password_hash': None,  # No password for OAuth users
                        'oauth_accounts': [{
                            'provider': 'google',
                            'provider_user_id': user_info.provider_user_id,
                            'linked_at': datetime.utcnow(),
                            'profile': user_info.profile
                        }],
                        'profile': default_profile or {},
                        'security': {
                            'failed_login_attempts': 0,
                            'locked_until': None,
                            'last_login': datetime.utcnow(),
                            'last_password_change': None,
                            'password_reset_token': None,
                            'password_reset_expires': None
                        },
                        'created_at': datetime.utcnow(),
                        'updated_at': datetime.utcnow()
                    }
                    
                    result = db['users_v2'].insert_one(user_doc)
                    
                    # Log user creation
                    db['auth_audit_log'].insert_one({
                        'event_type': 'user_registered',
                        'user_id': result.inserted_id,
                        'email': user_info.email,
                        'provider': 'google',
                        'success': True,
                        'timestamp': datetime.utcnow()
                    })
                    
                    logger.info(f"Created new user via Google OAuth: {user_info.email}")
                    
                    return True, str(result.inserted_id), None
                    
        except Exception as e:
            log_error("Google user provisioning", e)
            return False, None, str(e)
    
    def authenticate(self, credentials: AuthCredentials) -> AuthResult:
        """
        Authenticate user with Google OAuth.
        
        Credentials should contain:
        - authorization_code: Code from OAuth callback
        - code_verifier: PKCE code verifier
        - state: State parameter for CSRF validation
        
        Args:
            credentials: OAuth credentials
            
        Returns:
            AuthResult with success status
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            authorization_code = credentials.data.get('authorization_code')
            code_verifier = credentials.data.get('code_verifier')
            
            if not authorization_code or not code_verifier:
                return AuthResult(
                    success=False,
                    error_message="Missing authorization code or code verifier"
                )
            
            # Exchange code for tokens
            success, tokens, error = self.exchange_code_for_tokens(
                authorization_code,
                code_verifier
            )
            
            if not success:
                return AuthResult(
                    success=False,
                    error_message=f"Token exchange failed: {error}"
                )
            
            # Validate ID token
            success, user_info, error = self.validate_id_token(tokens['id_token'])
            
            if not success:
                return AuthResult(
                    success=False,
                    error_message=f"ID token validation failed: {error}"
                )
            
            # Provision or link user
            success, user_id, error = self.provision_or_link_user(user_info)
            
            if not success:
                return AuthResult(
                    success=False,
                    error_message=f"User provisioning failed: {error}"
                )
            
            # Log successful OAuth login
            with DatabaseConnectionContext(self.database.get_client()) as db:
                db['auth_audit_log'].insert_one({
                    'event_type': 'login_success',
                    'user_id': ObjectId(user_id),
                    'email': user_info.email,
                    'provider': 'google',
                    'ip_address': credentials.data.get('ip_address', 'unknown'),
                    'success': True,
                    'timestamp': datetime.utcnow()
                })
            
            logger.info(f"User authenticated via Google OAuth: {user_info.email}")
            
            return AuthResult(
                success=True,
                user_id=user_id,
                email=user_info.email,
                metadata={'google_tokens': tokens}
            )
            
        except Exception as e:
            log_error("Google OAuth authentication", e)
            return AuthResult(
                success=False,
                error_message="OAuth authentication failed due to server error"
            )
    
    def register(self, credentials: AuthCredentials, profile: Dict[str, Any]) -> AuthResult:
        """
        Register via Google OAuth (same as authenticate for OAuth).
        
        Args:
            credentials: OAuth credentials
            profile: User profile data
            
        Returns:
            AuthResult with success status
        """
        # For OAuth, registration is the same as authentication
        # User is auto-provisioned on first login
        return self.authenticate(credentials)
