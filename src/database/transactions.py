"""
MongoDB Transaction Manager - ACID Guarantees
Provides transaction support for multi-document operations in MongoDB.
"""

import pymongo
import pymongo.results
import pymongo.cursor
from pymongo.client_session import ClientSession
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from pymongo.read_preferences import ReadPreference
from typing import Optional, Callable, Any, Dict, List
from datetime import datetime
from contextlib import contextmanager
import logging
import time
from functools import wraps
from src.utils.helpers import log_error


logger = logging.getLogger(__name__)


class TransactionError(Exception):
    """Base exception for transaction errors."""
    pass


class VersionConflictError(TransactionError):
    """Raised when optimistic locking detects a version conflict."""
    pass


class TransactionTimeoutError(TransactionError):
    """Raised when transaction times out."""
    pass


class MongoTransaction:
    """
    Context manager for MongoDB transactions with ACID guarantees.
    
    Usage:
        with MongoTransaction(db_client) as txn:
            txn.update_one('users', {'username': 'alice'}, {'$set': {'age': 30}})
            txn.insert_one('audit_log', {'action': 'update', 'user': 'alice'})
            txn.commit()
        # Auto-rollback on exception
    """
    
    def __init__(self, client: pymongo.MongoClient, database_name: str = "SmartKitchen",
                 timeout_seconds: int = 30):
        """
        Initialize transaction.
        
        Args:
            client: MongoDB client (must be connected to replica set)
            database_name: Database name
            timeout_seconds: Transaction timeout in seconds
        """
        self.client = client
        self.database_name = database_name
        self.timeout_seconds = timeout_seconds
        self.session: Optional[ClientSession] = None
        self.db = None
        self._committed = False
        self._aborted = False
        self._operations: List[Dict[str, Any]] = []
        self._start_time = None
    
    def __enter__(self) -> 'MongoTransaction':
        """Start transaction."""
        try:
            # Start session with snapshot read concern for isolation
            self.session = self.client.start_session()
            
            # Start transaction with majority write concern for durability
            self.session.start_transaction(
                read_concern=ReadConcern('snapshot'),
                write_concern=WriteConcern('majority'),
                read_preference=ReadPreference.PRIMARY
            )
            
            self.db = self.client[self.database_name]
            self._start_time = time.time()
            
            logger.debug(f"Transaction started at {datetime.now()}")
            
            return self
        except Exception as e:
            log_error("start transaction", e)
            raise TransactionError(f"Failed to start transaction: {e}")
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """End transaction - commit or rollback."""
        try:
            if exc_type is not None:
                # Exception occurred - rollback
                self._rollback()
                logger.warning(f"Transaction rolled back due to exception: {exc_val}")
                return False  # Re-raise exception
            
            if not self._committed and not self._aborted:
                # Auto-commit if not explicitly committed/aborted
                self.commit()
            
            return True
        finally:
            if self.session:
                self.session.end_session()
    
    def _check_timeout(self) -> None:
        """Check if transaction has timed out."""
        if self._start_time and time.time() - self._start_time > self.timeout_seconds:
            raise TransactionTimeoutError(
                f"Transaction exceeded timeout of {self.timeout_seconds} seconds"
            )
    
    def update_one(self, collection: str, filter_doc: Dict, update_doc: Dict,
                   upsert: bool = False) -> pymongo.results.UpdateResult:
        """
        Update a single document within transaction.
        
        Args:
            collection: Collection name
            filter_doc: Filter to match document
            update_doc: Update operations
            upsert: Whether to insert if not found
            
        Returns:
            UpdateResult
        """
        self._check_timeout()
        
        try:
            result = self.db[collection].update_one(
                filter_doc, update_doc, upsert=upsert, session=self.session
            )
            
            self._operations.append({
                'type': 'update_one',
                'collection': collection,
                'filter': str(filter_doc),
                'matched': result.matched_count,
                'modified': result.modified_count
            })
            
            return result
        except Exception as e:
            log_error(f"transaction update_one in {collection}", e)
            raise
    
    def update_many(self, collection: str, filter_doc: Dict, update_doc: Dict) -> pymongo.results.UpdateResult:
        """
        Update multiple documents within transaction.
        
        Args:
            collection: Collection name
            filter_doc: Filter to match documents
            update_doc: Update operations
            
        Returns:
            UpdateResult
        """
        self._check_timeout()
        
        try:
            result = self.db[collection].update_many(
                filter_doc, update_doc, session=self.session
            )
            
            self._operations.append({
                'type': 'update_many',
                'collection': collection,
                'filter': str(filter_doc),
                'matched': result.matched_count,
                'modified': result.modified_count
            })
            
            return result
        except Exception as e:
            log_error(f"transaction update_many in {collection}", e)
            raise
    
    def insert_one(self, collection: str, document: Dict) -> pymongo.results.InsertOneResult:
        """
        Insert a document within transaction.
        
        Args:
            collection: Collection name
            document: Document to insert
            
        Returns:
            InsertOneResult
        """
        self._check_timeout()
        
        try:
            result = self.db[collection].insert_one(document, session=self.session)
            
            self._operations.append({
                'type': 'insert_one',
                'collection': collection,
                'inserted_id': str(result.inserted_id)
            })
            
            return result
        except Exception as e:
            log_error(f"transaction insert_one in {collection}", e)
            raise
    
    def insert_many(self, collection: str, documents: List[Dict]) -> pymongo.results.InsertManyResult:
        """
        Insert multiple documents within transaction.
        
        Args:
            collection: Collection name
            documents: Documents to insert
            
        Returns:
            InsertManyResult
        """
        self._check_timeout()
        
        try:
            result = self.db[collection].insert_many(documents, session=self.session)
            
            self._operations.append({
                'type': 'insert_many',
                'collection': collection,
                'count': len(result.inserted_ids)
            })
            
            return result
        except Exception as e:
            log_error(f"transaction insert_many in {collection}", e)
            raise
    
    def find_one(self, collection: str, filter_doc: Dict) -> Optional[Dict]:
        """
        Find a single document within transaction (with snapshot isolation).
        
        Args:
            collection: Collection name
            filter_doc: Filter to match document
            
        Returns:
            Document or None
        """
        self._check_timeout()
        
        try:
            return self.db[collection].find_one(filter_doc, session=self.session)
        except Exception as e:
            log_error(f"transaction find_one in {collection}", e)
            raise
    
    def find(self, collection: str, filter_doc: Dict) -> pymongo.cursor.Cursor:
        """
        Find documents within transaction (with snapshot isolation).
        
        Args:
            collection: Collection name
            filter_doc: Filter to match documents
            
        Returns:
            Cursor
        """
        self._check_timeout()
        
        try:
            return self.db[collection].find(filter_doc, session=self.session)
        except Exception as e:
            log_error(f"transaction find in {collection}", e)
            raise
    
    def delete_one(self, collection: str, filter_doc: Dict) -> pymongo.results.DeleteResult:
        """
        Delete a single document within transaction.
        
        Args:
            collection: Collection name
            filter_doc: Filter to match document
            
        Returns:
            DeleteResult
        """
        self._check_timeout()
        
        try:
            result = self.db[collection].delete_one(filter_doc, session=self.session)
            
            self._operations.append({
                'type': 'delete_one',
                'collection': collection,
                'deleted': result.deleted_count
            })
            
            return result
        except Exception as e:
            log_error(f"transaction delete_one in {collection}", e)
            raise
    
    def commit(self) -> None:
        """Commit the transaction."""
        if self._committed:
            logger.warning("Transaction already committed")
            return
        
        if self._aborted:
            raise TransactionError("Cannot commit aborted transaction")
        
        try:
            self.session.commit_transaction()
            self._committed = True
            
            duration = time.time() - self._start_time if self._start_time else 0
            logger.info(f"Transaction committed successfully in {duration:.3f}s with {len(self._operations)} operations")
        except Exception as e:
            log_error("commit transaction", e)
            self._rollback()
            raise TransactionError(f"Failed to commit transaction: {e}")
    
    def _rollback(self) -> None:
        """Rollback the transaction."""
        if self._aborted:
            return
        
        try:
            if self.session and self.session.in_transaction:
                self.session.abort_transaction()
            self._aborted = True
            logger.info(f"Transaction rolled back ({len(self._operations)} operations discarded)")
        except Exception as e:
            log_error("rollback transaction", e)
    
    def get_operations_summary(self) -> List[Dict[str, Any]]:
        """Get summary of operations performed in this transaction."""
        return self._operations.copy()


class TransactionManager:
    """
    High-level transaction manager with retry logic and helpers.
    """
    
    def __init__(self, client: pymongo.MongoClient, database_name: str = "SmartKitchen"):
        """
        Initialize transaction manager.
        
        Args:
            client: MongoDB client
            database_name: Database name
        """
        self.client = client
        self.database_name = database_name
    
    @contextmanager
    def transaction(self, timeout_seconds: int = 30):
        """
        Create a transaction context.
        
        Args:
            timeout_seconds: Transaction timeout
            
        Yields:
            MongoTransaction instance
        """
        with MongoTransaction(self.client, self.database_name, timeout_seconds) as txn:
            yield txn
    
    def execute_in_transaction(self, func: Callable[[MongoTransaction], Any],
                              max_retries: int = 3) -> Any:
        """
        Execute a function within a transaction with automatic retry.
        
        Args:
            func: Function that takes MongoTransaction and performs operations
            max_retries: Maximum retry attempts for transient errors
            
        Returns:
            Result from func
            
        Raises:
            TransactionError: If transaction fails after retries
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                with self.transaction() as txn:
                    result = func(txn)
                    txn.commit()
                    return result
            except pymongo.errors.ConnectionFailure as e:
                # Transient error - retry
                last_error = e
                logger.warning(f"Transaction attempt {attempt + 1} failed with connection error, retrying...")
                time.sleep(0.1 * (attempt + 1))  # Exponential backoff
            except pymongo.errors.OperationFailure as e:
                # Check if retryable
                if e.has_error_label("TransientTransactionError"):
                    last_error = e
                    logger.warning(f"Transaction attempt {attempt + 1} failed with transient error, retrying...")
                    time.sleep(0.1 * (attempt + 1))
                else:
                    # Not retryable
                    raise TransactionError(f"Transaction failed: {e}")
            except Exception as e:
                # Non-retryable error
                raise TransactionError(f"Transaction failed: {e}")
        
        # All retries exhausted
        raise TransactionError(f"Transaction failed after {max_retries} attempts: {last_error}")


def retry_on_transient_error(max_attempts: int = 3):
    """
    Decorator to retry operations on transient transaction errors.
    
    Args:
        max_attempts: Maximum retry attempts
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except pymongo.errors.ConnectionFailure as e:
                    last_error = e
                    logger.warning(f"Attempt {attempt + 1} failed, retrying...")
                    time.sleep(0.1 * (attempt + 1))
                except pymongo.errors.OperationFailure as e:
                    if e.has_error_label("TransientTransactionError"):
                        last_error = e
                        logger.warning(f"Attempt {attempt + 1} failed, retrying...")
                        time.sleep(0.1 * (attempt + 1))
                    else:
                        raise
            
            raise TransactionError(f"Operation failed after {max_attempts} attempts: {last_error}")
        
        return wrapper
    return decorator


def check_replica_set_support(client: pymongo.MongoClient) -> bool:
    """
    Check if MongoDB deployment supports transactions (replica set required).
    
    Args:
        client: MongoDB client
        
    Returns:
        True if transactions are supported
    """
    try:
        # Check if running as replica set
        is_master = client.admin.command('isMaster')
        
        # Replica set or sharded cluster supports transactions
        if 'setName' in is_master or 'msg' in is_master:
            return True
        
        return False
    except Exception as e:
        log_error("check replica set support", e)
        return False
