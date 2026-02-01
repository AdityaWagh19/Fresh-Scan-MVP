"""
Authentication configuration management.

Loads authentication settings from environment variables with secure defaults.
"""

import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables (override any existing ones)
load_dotenv(override=True)


class AuthConfig:
    """Authentication configuration from environment variables."""
    
    # Google OAuth Configuration
    GOOGLE_CLIENT_ID: str = os.getenv('GOOGLE_CLIENT_ID', '')
    GOOGLE_CLIENT_SECRET: str = os.getenv('GOOGLE_CLIENT_SECRET', '')
    GOOGLE_REDIRECT_URI: str = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:8080/auth/callback')
    
    # JWT Configuration
    JWT_SECRET_KEY: str = os.getenv('JWT_SECRET_KEY', '')
    JWT_ALGORITHM: str = 'HS256'
    JWT_ACCESS_TOKEN_EXPIRY: int = int(os.getenv('JWT_ACCESS_TOKEN_EXPIRY', '900'))  # 15 minutes
    JWT_REFRESH_TOKEN_EXPIRY: int = int(os.getenv('JWT_REFRESH_TOKEN_EXPIRY', '2592000'))  # 30 days
    JWT_RESET_TOKEN_EXPIRY: int = int(os.getenv('JWT_RESET_TOKEN_EXPIRY', '3600'))  # 1 hour
    
    # Security Configuration
    BCRYPT_COST_FACTOR: int = int(os.getenv('BCRYPT_COST_FACTOR', '12'))
    MAX_LOGIN_ATTEMPTS: int = int(os.getenv('MAX_LOGIN_ATTEMPTS', '5'))
    LOCKOUT_DURATION: int = int(os.getenv('LOCKOUT_DURATION', '1800'))  # 30 minutes
    
    # Rate Limiting
    RATE_LIMIT_LOGIN: int = int(os.getenv('RATE_LIMIT_LOGIN', '5'))  # per 15 minutes
    RATE_LIMIT_PASSWORD_RESET: int = int(os.getenv('RATE_LIMIT_PASSWORD_RESET', '3'))  # per hour
    RATE_LIMIT_TOKEN_REFRESH: int = int(os.getenv('RATE_LIMIT_TOKEN_REFRESH', '10'))  # per minute
    
    # Feature Flags
    ENABLE_GOOGLE_OAUTH: bool = os.getenv('ENABLE_GOOGLE_OAUTH', 'true').lower() == 'true'
    ENABLE_EMAIL_AUTH: bool = os.getenv('ENABLE_EMAIL_AUTH', 'true').lower() == 'true'
    REQUIRE_EMAIL_VERIFICATION: bool = os.getenv('REQUIRE_EMAIL_VERIFICATION', 'false').lower() == 'true'
    
    @classmethod
    def validate(cls) -> tuple[bool, list[str]]:
        """
        Validate configuration.
        
        Returns:
            Tuple of (is_valid, error_messages)
        """
        errors = []
        
        # Validate JWT secret key
        if not cls.JWT_SECRET_KEY:
            errors.append("JWT_SECRET_KEY is required. Generate with: python -c 'import secrets; print(secrets.token_hex(32))'")
        elif len(cls.JWT_SECRET_KEY) < 32:
            errors.append("JWT_SECRET_KEY must be at least 32 characters")
        
        # Validate Google OAuth if enabled
        if cls.ENABLE_GOOGLE_OAUTH:
            if not cls.GOOGLE_CLIENT_ID:
                errors.append("GOOGLE_CLIENT_ID is required when OAuth is enabled")
            if not cls.GOOGLE_CLIENT_SECRET:
                errors.append("GOOGLE_CLIENT_SECRET is required when OAuth is enabled")
        
        # Validate security settings
        if cls.BCRYPT_COST_FACTOR < 10:
            errors.append("BCRYPT_COST_FACTOR must be at least 10 for security")
        
        if cls.MAX_LOGIN_ATTEMPTS < 3:
            errors.append("MAX_LOGIN_ATTEMPTS should be at least 3")
        
        return len(errors) == 0, errors
    
    @classmethod
    def is_configured(cls) -> bool:
        """Check if minimum configuration is present."""
        return bool(cls.JWT_SECRET_KEY)
    
    @classmethod
    def get_oauth_enabled_providers(cls) -> list[str]:
        """Get list of enabled OAuth providers."""
        providers = []
        if cls.ENABLE_GOOGLE_OAUTH and cls.GOOGLE_CLIENT_ID:
            providers.append('google')
        return providers


# Validate configuration on import
_is_valid, _errors = AuthConfig.validate()
if not _is_valid:
    import warnings
    for error in _errors:
        warnings.warn(f"Auth configuration error: {error}", UserWarning)
