"""
Merged module from:
  - src/auth/core/oauth_callback_server.py
  - src/auth/core/pkce.py
  - src/auth/core/token_manager.py
  - src/auth/core/authentication_service.py
"""

# Imports
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from src.auth.config import AuthConfig
from src.utils.helpers import log_error
from typing import Optional, Dict, Any, TYPE_CHECKING
from urllib.parse import urlparse, parse_qs
import base64
import hashlib
import jwt
import logging
import secrets
import threading
import webbrowser

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from src.auth.providers import AuthProvider, AuthCredentials, AuthResult
    from src.database import DatabaseStateMachine, DatabaseConnectionContext


# ===== From src/auth/core/oauth_callback_server.py =====

logger = logging.getLogger(__name__)


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP request handler for OAuth callbacks."""
    
    # Class variable to store callback data
    callback_data: Optional[Dict[str, str]] = None
    
    def do_GET(self):
        """Handle GET request (OAuth callback)."""
        print(f"[DEBUG] Callback received: {self.path}")
        
        # Parse query parameters
        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)
        
        print(f"[DEBUG] Parsed path: {parsed_url.path}")
        print(f"[DEBUG] Query params: {list(params.keys())}")
        
        # Extract code and state
        code = params.get('code', [None])[0]
        state = params.get('state', [None])[0]
        error = params.get('error', [None])[0]
        
        if error:
            # OAuth error
            OAuthCallbackHandler.callback_data = {
                'error': error,
                'error_description': params.get('error_description', ['Unknown error'])[0]
            }
            
            # Send error response
            self.send_response(400)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <head><title>Authentication Failed</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: #d32f2f;">Authentication Failed</h1>
                    <p>There was an error during authentication.</p>
                    <p>You can close this window and return to the application.</p>
                </body>
                </html>
            """)
        elif code and state:
            # Successful callback
            OAuthCallbackHandler.callback_data = {
                'code': code,
                'state': state
            }
            
            # Send success response
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <head><title>Authentication Successful</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: #4caf50;">Authentication Successful!</h1>
                    <p>You have been successfully authenticated.</p>
                    <p>You can close this window and return to the application.</p>
                    <script>
                        setTimeout(function() { window.close(); }, 3000);
                    </script>
                </body>
                </html>
            """)
        else:
            # Invalid callback
            self.send_response(400)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <head><title>Invalid Request</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                    <h1 style="color: #ff9800;">Invalid Request</h1>
                    <p>The authentication callback is invalid.</p>
                </body>
                </html>
            """)
    
    def log_message(self, format, *args):
        """Log HTTP requests for debugging."""
        print(f"[HTTP] {format % args}")


class LocalOAuthCallbackServer:
    """
    Local HTTP server for OAuth callbacks.
    
    Runs temporarily to receive OAuth callback from browser.
    """
    
    def __init__(self, port: int = 8080):
        """
        Initialize callback server.
        
        Args:
            port: Port to listen on
        """
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
    
    def start(self) -> None:
        """Start the callback server in a background thread."""
        try:
            self.server = HTTPServer(('localhost', self.port), OAuthCallbackHandler)
            self.server_thread = threading.Thread(target=self.server.serve_forever)
            self.server_thread.daemon = True
            self.server_thread.start()
            logger.info(f"OAuth callback server started on port {self.port}")
        except Exception as e:
            logger.error(f"Failed to start callback server: {e}")
            raise
    
    def stop(self) -> None:
        """Stop the callback server."""
        print("[DEBUG] LocalOAuthCallbackServer.stop() called")
        if self.server:
            print("[DEBUG] Shutting down HTTP server...")
            
            # Shutdown in a separate thread to avoid blocking
            def shutdown_server():
                try:
                    self.server.shutdown()
                    print("[DEBUG] Server shutdown complete")
                except Exception as e:
                    print(f"[DEBUG] Server shutdown error: {e}")
            
            shutdown_thread = threading.Thread(target=shutdown_server)
            shutdown_thread.daemon = True
            shutdown_thread.start()
            
            # Wait for shutdown with timeout
            shutdown_thread.join(timeout=2.0)
            if shutdown_thread.is_alive():
                print("[DEBUG] Server shutdown timed out (this is OK)")
            
            print("[DEBUG] Closing server socket...")
            try:
                self.server.server_close()
                print("[DEBUG] Server socket closed")
            except Exception as e:
                print(f"[DEBUG] Server close error: {e}")
            
            logger.info("OAuth callback server stopped")
        else:
            print("[DEBUG] No server to stop")
        print("[DEBUG] LocalOAuthCallbackServer.stop() complete")
    
    def wait_for_callback(self, timeout: int = 300) -> Optional[Dict[str, str]]:
        """
        Wait for OAuth callback.
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Returns:
            Callback data dict or None if timeout
        """
        import time
        
        # Reset callback data
        OAuthCallbackHandler.callback_data = None
        
        # Wait for callback
        start_time = time.time()
        while time.time() - start_time < timeout:
            if OAuthCallbackHandler.callback_data is not None:
                return OAuthCallbackHandler.callback_data
            time.sleep(0.5)
        
        return None


def open_browser_for_oauth(authorization_url: str) -> bool:
    """
    Open browser for OAuth authorization.
    
    Args:
        authorization_url: OAuth authorization URL
        
    Returns:
        True if browser opened successfully
    """
    try:
        webbrowser.open(authorization_url)
        return True
    except Exception as e:
        logger.error(f"Failed to open browser: {e}")
        return False

# ===== From src/auth/core/pkce.py =====

def generate_code_verifier(length: int = 128) -> str:
    """
    Generate a cryptographically random code verifier.
    
    Args:
        length: Length of verifier (43-128 characters)
        
    Returns:
        URL-safe base64 encoded random string
    """
    if length < 43 or length > 128:
        raise ValueError("Code verifier length must be between 43 and 128")
    
    # Generate random bytes
    random_bytes = secrets.token_bytes(length)
    
    # Base64 URL-safe encode and remove padding
    verifier = base64.urlsafe_b64encode(random_bytes).decode('utf-8')
    verifier = verifier.rstrip('=')
    
    # Truncate to requested length
    return verifier[:length]


def generate_code_challenge(verifier: str, method: str = 'S256') -> str:
    """
    Generate code challenge from verifier.
    
    Args:
        verifier: Code verifier string
        method: Challenge method ('S256' or 'plain')
        
    Returns:
        Code challenge string
    """
    if method == 'S256':
        # SHA256 hash the verifier
        digest = hashlib.sha256(verifier.encode('utf-8')).digest()
        # Base64 URL-safe encode and remove padding
        challenge = base64.urlsafe_b64encode(digest).decode('utf-8')
        return challenge.rstrip('=')
    elif method == 'plain':
        # Plain method just returns the verifier
        return verifier
    else:
        raise ValueError(f"Unsupported challenge method: {method}")


def generate_state() -> str:
    """
    Generate a random state parameter for CSRF protection.
    
    Returns:
        Random state string
    """
    return secrets.token_urlsafe(32)


class PKCESession:
    """PKCE session data for OAuth flow."""
    
    def __init__(self):
        """Initialize PKCE session with generated values."""
        self.code_verifier = generate_code_verifier()
        self.code_challenge = generate_code_challenge(self.code_verifier)
        self.state = generate_state()
        self.challenge_method = 'S256'
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            'code_verifier': self.code_verifier,
            'code_challenge': self.code_challenge,
            'state': self.state,
            'challenge_method': self.challenge_method
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PKCESession':
        """Create from dictionary."""
        session = cls.__new__(cls)
        session.code_verifier = data['code_verifier']
        session.code_challenge = data['code_challenge']
        session.state = data['state']
        session.challenge_method = data.get('challenge_method', 'S256')
        return session

# ===== From src/auth/core/token_manager.py =====

class TokenType(Enum):
    """JWT token types."""
    ACCESS = "access"
    REFRESH = "refresh"
    RESET = "reset"


@dataclass
class TokenClaims:
    """JWT token claims."""
    sub: str  # Subject (user_id)
    email: str
    iat: int  # Issued at
    exp: int  # Expiry
    jti: str  # JWT ID (unique token identifier)
    type: TokenType
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TokenClaims':
        """Create TokenClaims from dictionary."""
        return cls(
            sub=data['sub'],
            email=data['email'],
            iat=data['iat'],
            exp=data['exp'],
            jti=data['jti'],
            type=TokenType(data['type'])
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JWT encoding."""
        return {
            'sub': self.sub,
            'email': self.email,
            'iat': self.iat,
            'exp': self.exp,
            'jti': self.jti,
            'type': self.type.value
        }


class TokenManager:
    """
    JWT token management with secure signing and validation.
    
    Token Types:
    - Access Token: Short-lived (15 min), used for API requests
    - Refresh Token: Long-lived (30 days), used to get new access tokens
    - Reset Token: One-time use (1 hour), for password resets
    """
    
    def __init__(self, secret_key: Optional[str] = None):
        """
        Initialize token manager.
        
        Args:
            secret_key: JWT signing key (defaults to config)
        """
        self.secret_key = secret_key or AuthConfig.JWT_SECRET_KEY
        self.algorithm = AuthConfig.JWT_ALGORITHM
        
        # Expiry times (can be overridden for testing)
        self.access_token_expiry = AuthConfig.JWT_ACCESS_TOKEN_EXPIRY
        self.refresh_token_expiry = AuthConfig.JWT_REFRESH_TOKEN_EXPIRY
        self.reset_token_expiry = AuthConfig.JWT_RESET_TOKEN_EXPIRY
        
        if not self.secret_key:
            raise ValueError("JWT_SECRET_KEY must be configured")
    
    def generate_access_token(self, user_id: str, email: str) -> str:
        """
        Generate access token.
        
        Args:
            user_id: User identifier
            email: User email
            
        Returns:
            Encoded JWT access token
        """
        import time
        now_timestamp = int(time.time())
        expiry_timestamp = now_timestamp + self.access_token_expiry
        
        claims = TokenClaims(
            sub=user_id,
            email=email,
            iat=now_timestamp,
            exp=expiry_timestamp,
            jti=secrets.token_hex(16),
            type=TokenType.ACCESS
        )
        
        return jwt.encode(
            claims.to_dict(),
            self.secret_key,
            algorithm=self.algorithm
        )
    
    def generate_refresh_token(self, user_id: str, email: str) -> str:
        """
        Generate refresh token.
        
        Args:
            user_id: User identifier
            email: User email
            
        Returns:
            Encoded JWT refresh token
        """
        import time
        now_timestamp = int(time.time())
        expiry_timestamp = now_timestamp + self.refresh_token_expiry
        
        claims = TokenClaims(
            sub=user_id,
            email=email,
            iat=now_timestamp,
            exp=expiry_timestamp,
            jti=secrets.token_hex(16),
            type=TokenType.REFRESH
        )
        
        return jwt.encode(
            claims.to_dict(),
            self.secret_key,
            algorithm=self.algorithm
        )
    
    def generate_reset_token(self, user_id: str, email: str) -> str:
        """
        Generate password reset token.
        
        Args:
            user_id: User identifier
            email: User email
            
        Returns:
            Encoded JWT reset token
        """
        import time
        now_timestamp = int(time.time())
        expiry_timestamp = now_timestamp + self.reset_token_expiry
        
        claims = TokenClaims(
            sub=user_id,
            email=email,
            iat=now_timestamp,
            exp=expiry_timestamp,
            jti=secrets.token_hex(16),
            type=TokenType.RESET
        )
        
        return jwt.encode(
            claims.to_dict(),
            self.secret_key,
            algorithm=self.algorithm
        )
    
    def validate_token(self, token: str, expected_type: TokenType) -> Optional[TokenClaims]:
        """
        Validate JWT token.
        
        Args:
            token: JWT token string
            expected_type: Expected token type
            
        Returns:
            TokenClaims if valid, None otherwise
        """
        try:
            # Decode and verify signature
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm]
            )
            
            # Parse claims
            claims = TokenClaims.from_dict(payload)
            
            # Verify token type
            if claims.type != expected_type:
                log_error("token validation", 
                         ValueError(f"Invalid token type. Expected {expected_type}, got {claims.type}"))
                return None
            
            # Check expiry (jwt.decode already checks this, but explicit check)
            if datetime.utcnow().timestamp() > claims.exp:
                return None
            
            return claims
            
        except jwt.ExpiredSignatureError:
            log_error("token validation", ValueError("Token has expired"))
            return None
        except jwt.InvalidTokenError as e:
            log_error("token validation", e)
            return None
        except Exception as e:
            log_error("token validation", e)
            return None
    
    def decode_token_unsafe(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Decode token without validation (for debugging/logging).
        
        Args:
            token: JWT token string
            
        Returns:
            Decoded payload or None
        """
        try:
            return jwt.decode(
                token,
                options={"verify_signature": False}
            )
        except Exception as e:
            log_error("token decode", e)
            return None
    
    @staticmethod
    def generate_secure_key() -> str:
        """
        Generate a secure random key for JWT signing.
        
        Returns:
            256-bit hex-encoded random key
        """
        return secrets.token_hex(32)


@dataclass
class TokenPair:
    """Pair of access and refresh tokens."""
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = AuthConfig.JWT_ACCESS_TOKEN_EXPIRY
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'token_type': self.token_type,
            'expires_in': self.expires_in
        }


# Global token manager instance
_token_manager: Optional[TokenManager] = None


def get_token_manager() -> TokenManager:
    """Get global token manager instance."""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
    return _token_manager

# ===== From src/auth/core/authentication_service.py =====

from src.auth.config import AuthConfig
from src.utils.helpers import log_error
from bson import ObjectId

logger = logging.getLogger(__name__)


class AuthenticationService:
    """
    Central authentication service supporting multiple providers.
    
    Features:
    - Strategy pattern for auth providers (Email, Google OAuth, future providers)
    - JWT token generation and validation
    - Session lifecycle management
    - Rate limiting and security controls
    """
    
    def __init__(self, database: 'DatabaseStateMachine'):
        """
        Initialize authentication service.
        
        Args:
            database: Database connection
        """
        self.database = database
        self.token_manager = get_token_manager()
        self.providers: Dict[str, Any] = {}
        
        # Register email/password provider (import here to avoid circular dependency)
        from src.auth.providers import EmailPasswordProvider
        self.register_provider(EmailPasswordProvider(database))
        
        # Register Google OAuth provider if enabled
        if AuthConfig.ENABLE_GOOGLE_OAUTH:
            try:
                from src.auth.providers import GoogleOAuthProvider
                google_provider = GoogleOAuthProvider(database)
                if google_provider.is_configured():
                    self.register_provider(google_provider)
                else:
                    logger.warning("Google OAuth enabled but not configured")
            except ImportError as e:
                logger.error(f"Failed to import Google OAuth provider: {e}")
    
    def register_provider(self, provider: 'AuthProvider') -> None:
        """
        Register an authentication provider.
        
        Args:
            provider: Authentication provider instance
        """
        self.providers[provider.provider_name] = provider
        logger.info(f"Registered auth provider: {provider.provider_name}")
    
    def get_provider(self, provider_name: str) -> Optional['AuthProvider']:
        """
        Get authentication provider by name.
        
        Args:
            provider_name: Provider name (e.g., 'email', 'google')
            
        Returns:
            AuthProvider instance or None
        """
        return self.providers.get(provider_name)
    
    def register_user(
        self,
        provider_name: str,
        credentials: 'AuthCredentials',
        profile: Dict[str, Any]
    ) -> tuple['AuthResult', Optional[TokenPair]]:
        """
        Register new user with specified provider.
        
        Args:
            provider_name: Provider name
            credentials: Registration credentials
            profile: User profile data
            
        Returns:
            Tuple of (AuthResult, TokenPair if successful)
        """
        try:
            provider = self.get_provider(provider_name)
            if not provider:
                return AuthResult(
                    success=False,
                    error_message=f"Unknown provider: {provider_name}"
                ), None
            
            # Register with provider
            result = provider.register(credentials, profile)
            
            if not result.success:
                return result, None
            
            # Generate tokens if registration successful and no verification required
            if not result.requires_verification:
                tokens = self._create_session(result.user_id, result.email, credentials.data)
                return result, tokens
            
            return result, None
            
        except Exception as e:
            log_error("user registration", e)
            return AuthResult(
                success=False,
                error_message="Registration failed due to server error"
            ), None
    
    def authenticate_user(
        self,
        provider_name: str,
        credentials: 'AuthCredentials'
    ) -> tuple['AuthResult', Optional[TokenPair]]:
        """
        Authenticate user with specified provider.
        
        Args:
            provider_name: Provider name
            credentials: Authentication credentials
            
        Returns:
            Tuple of (AuthResult, TokenPair if successful)
        """
        try:
            provider = self.get_provider(provider_name)
            if not provider:
                return AuthResult(
                    success=False,
                    error_message=f"Unknown provider: {provider_name}"
                ), None
            
            # Authenticate with provider
            result = provider.authenticate(credentials)
            
            if not result.success:
                return result, None
            
            # Generate tokens
            tokens = self._create_session(result.user_id, result.email, credentials.data)
            
            return result, tokens
            
        except Exception as e:
            log_error("user authentication", e)
            return AuthResult(
                success=False,
                error_message="Authentication failed due to server error"
            ), None
    
    def refresh_token(self, refresh_token: str) -> Optional[TokenPair]:
        """
        Refresh access token using refresh token.
        
        Args:
            refresh_token: Valid refresh token
            
        Returns:
            New TokenPair or None if invalid
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            # Validate refresh token
            claims = self.token_manager.validate_token(refresh_token, TokenType.REFRESH)
            if not claims:
                return None
            
            # Check if session is revoked
            with DatabaseConnectionContext(self.database.get_client()) as db:
                session = db['auth_sessions'].find_one({
                    'refresh_token_jti': claims.jti,
                    'revoked': False
                })
                
                if not session:
                    logger.warning(f"Attempted to use revoked refresh token: {claims.jti}")
                    return None
                
                # Generate new token pair
                new_access_token = self.token_manager.generate_access_token(
                    claims.sub,
                    claims.email
                )
                new_refresh_token = self.token_manager.generate_refresh_token(
                    claims.sub,
                    claims.email
                )
                
                # Decode new tokens to get JTIs
                new_access_claims = self.token_manager.validate_token(new_access_token, TokenType.ACCESS)
                new_refresh_claims = self.token_manager.validate_token(new_refresh_token, TokenType.REFRESH)
                
                # Update session with new tokens (token rotation)
                db['auth_sessions'].update_one(
                    {'_id': session['_id']},
                    {
                        '$set': {
                            'access_token_jti': new_access_claims.jti,
                            'refresh_token_jti': new_refresh_claims.jti,
                            'last_activity': datetime.utcnow()
                        }
                    }
                )
                
                # Log token refresh
                db['auth_audit_log'].insert_one({
                    'event_type': 'token_refreshed',
                    'user_id': session['user_id'],
                    'email': claims.email,
                    'success': True,
                    'timestamp': datetime.utcnow()
                })
                
                return TokenPair(
                    access_token=new_access_token,
                    refresh_token=new_refresh_token
                )
                
        except Exception as e:
            log_error("token refresh", e)
            return None
    
    def validate_session(self, access_token: str) -> Optional[Dict[str, Any]]:
        """
        Validate access token and return session info.
        
        Args:
            access_token: JWT access token
            
        Returns:
            Session info dict or None if invalid
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            # Validate token
            claims = self.token_manager.validate_token(access_token, TokenType.ACCESS)
            if not claims:
                return None
            
            # Check if session exists and is not revoked
            with DatabaseConnectionContext(self.database.get_client()) as db:
                session = db['auth_sessions'].find_one({
                    'access_token_jti': claims.jti,
                    'revoked': False
                })
                
                if not session:
                    return None
                
                # Update last activity
                db['auth_sessions'].update_one(
                    {'_id': session['_id']},
                    {'$set': {'last_activity': datetime.utcnow()}}
                )
                
                return {
                    'user_id': str(session['user_id']),
                    'email': claims.email,
                    'session_id': str(session['_id'])
                }
                
        except Exception as e:
            log_error("session validation", e)
            return None
    
    def revoke_token(self, token: str) -> bool:
        """
        Revoke a token (access or refresh).
        
        Args:
            token: JWT token to revoke
            
        Returns:
            True if revoked successfully
        """
        try:
            # Import here to avoid circular dependency
            from src.database import DatabaseConnectionContext
            
            # Try to decode token (don't validate expiry)
            payload = self.token_manager.decode_token_unsafe(token)
            if not payload:
                return False
            
            jti = payload.get('jti')
            if not jti:
                return False
            
            # Revoke session
            with DatabaseConnectionContext(self.database.get_client()) as db:
                result = db['auth_sessions'].update_one(
                    {
                        '$or': [
                            {'access_token_jti': jti},
                            {'refresh_token_jti': jti}
                        ]
                    },
                    {'$set': {'revoked': True}}
                )
                
                if result.modified_count > 0:
                    # Log token revocation
                    db['auth_audit_log'].insert_one({
                        'event_type': 'token_revoked',
                        'success': True,
                        'metadata': {'jti': jti},
                        'timestamp': datetime.utcnow()
                    })
                    
                    return True
                
                return False
                
        except Exception as e:
            log_error("token revocation", e)
            return False
    
    def logout(self, access_token: str) -> bool:
        """
        Logout user by revoking their session.
        
        Args:
            access_token: User's access token
            
        Returns:
            True if logged out successfully
        """
        return self.revoke_token(access_token)
    
    def _create_session(
        self,
        user_id: str,
        email: str,
        metadata: Dict[str, Any]
    ) -> TokenPair:
        """
        Create new session with JWT tokens.
        
        Args:
            user_id: User identifier
            email: User email
            metadata: Session metadata (IP, user agent, etc.)
            
        Returns:
            TokenPair with access and refresh tokens
        """
        # Generate NEW internal tokens (not Google tokens!)
        access_token = self.token_manager.generate_access_token(user_id, email)
        refresh_token = self.token_manager.generate_refresh_token(user_id, email)
        
        logger.info(f"Generated new tokens for user {email}")
        
        # Validate the tokens we just created
        access_claims = self.token_manager.validate_token(access_token, TokenType.ACCESS)
        refresh_claims = self.token_manager.validate_token(refresh_token, TokenType.REFRESH)
        
        # Guard clause: If token validation fails, something is seriously wrong
        if not access_claims or not refresh_claims:
            error_msg = "Failed to validate newly generated tokens"
            logger.error(f"{error_msg} for user {email}")
            logger.error(f"Access claims: {access_claims}, Refresh claims: {refresh_claims}")
            logger.error(f"Token expiry config - Access: {self.token_manager.access_token_expiry}, Refresh: {self.token_manager.refresh_token_expiry}")
            raise ValueError(error_msg)
        
        # Import here to avoid circular dependency
        from src.database import DatabaseConnectionContext
        
        # Create session record
        with DatabaseConnectionContext(self.database.get_client()) as db:
            session_doc = {
                'user_id': ObjectId(user_id),
                'access_token_jti': access_claims.jti,
                'refresh_token_jti': refresh_claims.jti,
                'device_info': {
                    'interface': metadata.get('interface', 'cli'),
                    'user_agent': metadata.get('user_agent', 'unknown'),
                    'ip_address': metadata.get('ip_address', 'unknown')
                },
                'created_at': datetime.utcnow(),
                'expires_at': datetime.fromtimestamp(refresh_claims.exp),
                'last_activity': datetime.utcnow(),
                'revoked': False
            }
            
            db['auth_sessions'].insert_one(session_doc)
            
            # Log token issuance
            db['auth_audit_log'].insert_one({
                'event_type': 'tokens_issued',
                'user_id': ObjectId(user_id),
                'email': email,
                'success': True,
                'timestamp': datetime.utcnow()
            })
        
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token
        )


# Global authentication service instance
_auth_service: Optional[AuthenticationService] = None


def get_auth_service(database: 'DatabaseStateMachine') -> AuthenticationService:
    """Get global authentication service instance."""
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthenticationService(database)
    return _auth_service