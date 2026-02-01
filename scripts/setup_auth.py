"""
Setup script for OAuth 2.0 authentication system.

Run this script to:
1. Generate JWT secret key
2. Create .env file with authentication settings
3. Create database indexes
"""

import secrets
import os
from pathlib import Path


def generate_jwt_secret():
    """Generate a secure JWT secret key."""
    return secrets.token_hex(32)


def create_env_file():
    """Create .env file from template."""
    template_path = Path('.env.auth.template')
    env_path = Path('.env')
    
    if not template_path.exists():
        print("ERROR: Template file not found: .env.auth.template")
        return False
    
    # Generate JWT secret
    jwt_secret = generate_jwt_secret()
    
    # Read template
    with open(template_path, 'r') as f:
        content = f.read()
    
    # Replace placeholder
    content = content.replace(
        'JWT_SECRET_KEY=your_secret_key_here_replace_with_generated_key',
        f'JWT_SECRET_KEY={jwt_secret}'
    )
    
    # Check if .env already exists
    if env_path.exists():
        print("\nWARNING:  .env file already exists")
        response = input("Do you want to append auth settings? (y/n): ")
        if response.lower() != 'y':
            print("Setup cancelled")
            return False
        
        # Append to existing .env
        with open(env_path, 'a') as f:
            f.write("\n\n# ============================================\n")
            f.write("# Authentication Settings (Auto-generated)\n")
            f.write("# ============================================\n\n")
            f.write(content)
        
        print("\nOK: Authentication settings appended to .env")
    else:
        # Create new .env
        with open(env_path, 'w') as f:
            f.write(content)
        
        print("\nOK: .env file created successfully")
    
    print(f"\n🔑 Generated JWT Secret Key: {jwt_secret}")
    print("\nWARNING:  IMPORTANT: Keep this key secure and never commit it to version control!")
    
    return True


def create_database_indexes():
    """Create database indexes for authentication."""
    print("\n" + "="*60)
    print("DATABASE INDEX CREATION")
    print("="*60)
    
    print("\nTo create database indexes, run the following MongoDB commands:")
    print("\n```javascript")
    print("// Connect to your MongoDB database")
    print("use smart_fridge")
    print()
    print("// Create unique index on email")
    print('db.users_v2.createIndex({ "email": 1 }, { unique: true })')
    print()
    print("// Create index on OAuth accounts")
    print('db.users_v2.createIndex({ "oauth_accounts.provider": 1, "oauth_accounts.provider_user_id": 1 })')
    print()
    print("// Create index on locked accounts")
    print('db.users_v2.createIndex({ "security.locked_until": 1 }, { sparse: true })')
    print()
    print("// Create index on session tokens")
    print('db.auth_sessions.createIndex({ "user_id": 1, "revoked": 1 })')
    print('db.auth_sessions.createIndex({ "access_token_jti": 1 }, { unique: true })')
    print()
    print("// Create TTL index on sessions (auto-delete expired)")
    print('db.auth_sessions.createIndex({ "expires_at": 1 }, { expireAfterSeconds: 0 })')
    print()
    print("// Create indexes on audit log")
    print('db.auth_audit_log.createIndex({ "user_id": 1, "timestamp": -1 })')
    print('db.auth_audit_log.createIndex({ "event_type": 1, "timestamp": -1 })')
    print('db.auth_audit_log.createIndex({ "ip_address": 1, "timestamp": -1 })')
    print("```")
    print()


def main():
    """Main setup function."""
    print("="*60)
    print("SMART FRIDGE - OAUTH 2.0 AUTHENTICATION SETUP")
    print("="*60)
    
    # Create .env file
    if not create_env_file():
        return
    
    # Show database index commands
    create_database_indexes()
    
    print("\n" + "="*60)
    print("SETUP COMPLETE")
    print("="*60)
    print("\nOK: Authentication system is ready to use!")
    print("\nNext steps:")
    print("1. Review and update .env file with your settings")
    print("2. Run the MongoDB index creation commands shown above")
    print("3. Install required packages: pip install PyJWT python-dotenv")
    print("4. Test the authentication system")
    print()


if __name__ == '__main__':
    main()
