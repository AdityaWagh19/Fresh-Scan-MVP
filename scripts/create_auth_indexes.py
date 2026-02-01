"""
Database index creation script for OAuth 2.0 authentication system.

Creates all necessary indexes for optimal query performance and data integrity.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.database import DatabaseStateMachine, DatabaseConnectionContext
from pymongo import ASCENDING, DESCENDING
from datetime import datetime


def create_users_v2_indexes(db):
    """Create indexes for users_v2 collection."""
    print("\n[*] Creating indexes for users_v2...")
    
    collection = db['users_v2']
    
    # 1. Unique index on email
    collection.create_index(
        [('email', ASCENDING)],
        unique=True,
        name='email_unique'
    )
    print("   [+] Created unique index on email")
    
    # 2. Compound index on OAuth accounts
    collection.create_index(
        [
            ('oauth_accounts.provider', ASCENDING),
            ('oauth_accounts.provider_user_id', ASCENDING)
        ],
        name='oauth_accounts_provider_id'
    )
    print("   [+] Created index on oauth_accounts.provider + provider_user_id")
    
    # 3. Sparse index on locked accounts
    collection.create_index(
        [('security.locked_until', ASCENDING)],
        sparse=True,
        name='security_locked_until'
    )
    print("   [+] Created sparse index on security.locked_until")
    
    # 4. Index on auth_provider for filtering
    collection.create_index(
        [('auth_provider', ASCENDING)],
        name='auth_provider'
    )
    print("   [+] Created index on auth_provider")
    
    # 5. Index on created_at for sorting
    collection.create_index(
        [('created_at', DESCENDING)],
        name='created_at_desc'
    )
    print("   [+] Created index on created_at")


def create_auth_sessions_indexes(db):
    """Create indexes for auth_sessions collection."""
    print("\n[*] Creating indexes for auth_sessions...")
    
    collection = db['auth_sessions']
    
    # 1. Compound index on user_id and revoked status
    collection.create_index(
        [
            ('user_id', ASCENDING),
            ('revoked', ASCENDING)
        ],
        name='user_sessions'
    )
    print("   [+] Created index on user_id + revoked")
    
    # 2. Unique index on access_token_jti
    collection.create_index(
        [('access_token_jti', ASCENDING)],
        unique=True,
        name='access_token_jti_unique'
    )
    print("   [+] Created unique index on access_token_jti")
    
    # 3. Index on refresh_token_jti
    collection.create_index(
        [('refresh_token_jti', ASCENDING)],
        name='refresh_token_jti'
    )
    print("   [+] Created index on refresh_token_jti")
    
    # 4. TTL index on expires_at (auto-delete expired sessions)
    collection.create_index(
        [('expires_at', ASCENDING)],
        expireAfterSeconds=0,
        name='expires_at_ttl'
    )
    print("   [+] Created TTL index on expires_at (auto-cleanup)")
    
    # 5. Index on created_at for sorting
    collection.create_index(
        [('created_at', DESCENDING)],
        name='created_at_desc'
    )
    print("   [+] Created index on created_at")


def create_auth_audit_log_indexes(db):
    """Create indexes for auth_audit_log collection."""
    print("\n[*] Creating indexes for auth_audit_log...")
    
    collection = db['auth_audit_log']
    
    # 1. Compound index on user_id and timestamp
    collection.create_index(
        [
            ('user_id', ASCENDING),
            ('timestamp', DESCENDING)
        ],
        name='user_audit_timeline'
    )
    print("   [+] Created index on user_id + timestamp")
    
    # 2. Compound index on event_type and timestamp
    collection.create_index(
        [
            ('event_type', ASCENDING),
            ('timestamp', DESCENDING)
        ],
        name='event_timeline'
    )
    print("   [+] Created index on event_type + timestamp")
    
    # 3. Compound index on IP address and timestamp (security monitoring)
    collection.create_index(
        [
            ('ip_address', ASCENDING),
            ('timestamp', DESCENDING)
        ],
        name='ip_timeline'
    )
    print("   [+] Created index on ip_address + timestamp")
    
    # 4. Index on email for lookups
    collection.create_index(
        [('email', ASCENDING)],
        name='email'
    )
    print("   [+] Created index on email")
    
    # 5. Index on success status
    collection.create_index(
        [('success', ASCENDING)],
        name='success'
    )
    print("   [+] Created index on success")
    
    # 6. TTL index on timestamp (auto-delete old logs after 90 days)
    collection.create_index(
        [('timestamp', ASCENDING)],
        expireAfterSeconds=7776000,  # 90 days
        name='timestamp_ttl'
    )
    print("   [+] Created TTL index on timestamp (90-day retention)")


def verify_indexes(db):
    """Verify all indexes were created successfully."""
    print("\n[*] Verifying indexes...")
    
    collections = {
        'users_v2': ['email_unique', 'oauth_accounts_provider_id', 'security_locked_until', 'auth_provider', 'created_at_desc'],
        'auth_sessions': ['user_sessions', 'access_token_jti_unique', 'refresh_token_jti', 'expires_at_ttl', 'created_at_desc'],
        'auth_audit_log': ['user_audit_timeline', 'event_timeline', 'ip_timeline', 'email', 'success', 'timestamp_ttl']
    }
    
    all_verified = True
    
    for collection_name, expected_indexes in collections.items():
        collection = db[collection_name]
        existing_indexes = collection.index_information()
        
        print(f"\n   {collection_name}:")
        for index_name in expected_indexes:
            if index_name in existing_indexes:
                print(f"      [+] {index_name}")
            else:
                print(f"      [-] {index_name} - NOT FOUND")
                all_verified = False
    
    return all_verified


def main():
    """Create all database indexes."""
    print("="*60)
    print("DATABASE INDEX CREATION - OAUTH 2.0 AUTH SYSTEM")
    print("="*60)
    
    try:
        # Initialize database with connection factory
        from src.config.constants import MONGO_URI
        import pymongo
        
        def create_mongo_client():
            return pymongo.MongoClient(MONGO_URI)
        
        db_state = DatabaseStateMachine(create_mongo_client)
        db_state.ensure_connected()
        
        with DatabaseConnectionContext(db_state.get_client()) as db:
            print(f"\nConnected to database: {db.name}")
            
            # Create indexes
            create_users_v2_indexes(db)
            create_auth_sessions_indexes(db)
            create_auth_audit_log_indexes(db)
            
            # Verify indexes
            if verify_indexes(db):
                print("\n" + "="*60)
                print("ALL INDEXES CREATED SUCCESSFULLY")
                print("="*60)
                print("\nIndex Summary:")
                print("   - users_v2: 5 indexes (1 unique, 1 sparse)")
                print("   - auth_sessions: 5 indexes (1 unique, 1 TTL)")
                print("   - auth_audit_log: 6 indexes (1 TTL)")
                print("\nPerformance optimizations:")
                print("   + Fast email lookups (unique index)")
                print("   + Fast OAuth account lookups")
                print("   + Fast session validation")
                print("   + Auto-cleanup of expired sessions")
                print("   + Auto-cleanup of old audit logs (90 days)")
                print("\nDatabase is ready for production use!")
            else:
                print("\nSome indexes failed to create")
                print("Please check the errors above")
        
    except Exception as e:
        print(f"\nError creating indexes: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
