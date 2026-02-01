"""
Merged module from:
  - src/integration/blinkit_integration.py
  - src/integration/blinkit_ordering.py
  - src/integration/blinkit_session_manager.py
  - src/integration/integration_status.py
  - src/integration/menu.py
  - src/integration/blinkit_mcp.py
"""

# Imports
from __future__ import annotations
from contextlib import redirect_stdout
from dataclasses import dataclass, asdict
from datetime import datetime
from datetime import datetime, timedelta
from dotenv import load_dotenv
from functools import wraps
from mcp.server.fastmcp import FastMCP
from pathlib import Path
from playwright.async_api import async_playwright, Page
from src.database.connection import DatabaseConnectionContext
from src.utils.helpers import log_error
from typing import Any
from typing import Dict, Any, List
from typing import List, Dict, Optional
from typing import Optional, Dict, Any
from typing import TYPE_CHECKING, Optional
import asyncio
import hashlib
import io
import json
import os
import sys
import threading
import urllib.request


# ===== From src/integration/blinkit_integration.py =====

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from src.integration.blinkit_mcp import BlinkitContext, BlinkitOrder
except ImportError:
    print("Warning: Could not import blinkit_mcp. Blinkit integration will be unavailable.")
    BlinkitContext = None
    BlinkitOrder = None

from src.utils.helpers import log_error
import threading
import weakref
import time
from collections import deque


# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class MaxRetriesExceededError(Exception):
    """Raised when maximum retry attempts are exceeded."""
    pass

class PageValidationError(Exception):
    """Raised when page validation fails."""
    pass

class CartVerificationError(Exception):
    """Raised when cart verification fails."""
    pass


# ============================================================================
# SERVICE INSTANCE POOL
# ============================================================================

class ServiceInstancePool:
    """Pool for managing reusable service instances."""
    
    def __init__(self, max_instances=2, idle_timeout=300):  # 5 minutes
        self.max_instances = max_instances
        self.idle_timeout = idle_timeout
        self.instances = deque()  # (instance, last_used_time)
        self.lock = threading.Lock()
        self._cleanup_timer = None
        
    def get_instance(self):
        """Get an available instance or create new one."""
        with self.lock:
            # Try to find a valid instance
            while self.instances:
                instance, last_used = self.instances.popleft()
                
                # Check if instance is still valid (weak reference)
                if instance is not None:
                    try:
                        # Basic validation - check if initialized
                        if hasattr(instance, '_initialized') and instance._initialized:
                            return instance
                    except:
                        pass
            
            # No valid instance found, create new if under limit
            if len(self.instances) < self.max_instances:
                return None  # Signal to create new instance
            
            return None
    
    def return_instance(self, instance):
        """Return instance to pool."""
        with self.lock:
            if len(self.instances) < self.max_instances:
                self.instances.append((instance, time.time()))
                self._schedule_cleanup()
    
    def _schedule_cleanup(self):
        """Schedule cleanup of idle instances."""
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
        
        self._cleanup_timer = threading.Timer(self.idle_timeout, self._cleanup_idle)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()
    
    def _cleanup_idle(self):
        """Remove instances that have been idle too long."""
        with self.lock:
            current_time = time.time()
            new_instances = deque()
            
            for instance, last_used in self.instances:
                if current_time - last_used < self.idle_timeout:
                    new_instances.append((instance, last_used))
                else:
                    # Close idle instance
                    try:
                        if hasattr(instance, 'close'):
                            # Schedule async close
                            pass
                    except:
                        pass
            
            self.instances = new_instances


# Global service pool
_service_pool = ServiceInstancePool(max_instances=2, idle_timeout=300)


# ============================================================================
# METRICS
# ============================================================================

class ServiceMetrics:
    """Track service metrics."""
    
    def __init__(self):
        self.retry_count = 0
        self.validation_failures = 0
        self.cart_verification_time = 0.0
        self.last_health_check = 0.0
        self.lock = threading.Lock()
    
    def increment_retry(self):
        with self.lock:
            self.retry_count += 1
    
    def increment_validation_failure(self):
        with self.lock:
            self.validation_failures += 1
    
    def record_cart_verification(self, duration):
        with self.lock:
            self.cart_verification_time = duration
    
    def update_health_check(self):
        with self.lock:
            self.last_health_check = time.time()
    
    def get_stats(self):
        with self.lock:
            return {
                'retry_count': self.retry_count,
                'validation_failures': self.validation_failures,
                'cart_verification_time': self.cart_verification_time,
                'last_health_check': self.last_health_check
            }


_metrics = ServiceMetrics()



class BlinkitIntegrationService:
    """Service to integrate grocery lists with Blinkit ordering."""
    
    def __init__(self, username: str):
        """
        Initialize Blinkit integration service for a specific user.
        
        Args:
            username: Smart Fridge username (for session isolation)
        """
        if not username:
            raise ValueError("Username is required for Blinkit integration")
        
        self.username = username  # CRITICAL: Tie service to specific user
        self.ctx = None
        self.order_manager = None
        self._initialized = False
        self._page_lock = asyncio.Lock()
        
        # Import session manager
        from src.services.integrations.blinkit import get_session_manager
        self.session_manager = get_session_manager()
        
        # Verbose logging moved to debug level
        import logging
        logging.debug(f"Blinkit service created for user: {username}")

    
    async def initialize(self):
        """Initialize the Blinkit context and order manager with per-user session."""
        if BlinkitContext is None:
            raise ImportError("blinkit_mcp module not available")
        
        try:
            # Check if existing context/page is tied to a closed event loop
            if hasattr(self, 'ctx') and self.ctx and self.ctx.auth:
                try:
                    # Check if page exists and is valid
                    if self.ctx.auth.page is not None:
                        try:
                            # Try to check if page is valid (this will fail if event loop is closed)
                            _ = self.ctx.auth.page.is_closed()
                        except (RuntimeError, AttributeError) as e:
                            # Event loop is closed or page is invalid, need to create new context
                            error_str = str(e).lower()
                            if "event loop is closed" in error_str or "closed" in error_str or "nonetype" in error_str:
                                import logging
                                logging.debug("Previous browser session tied to closed event loop. Creating new session...")
                                # Properly clean up old context
                                try:
                                    if hasattr(self.ctx.auth, 'page') and self.ctx.auth.page:
                                        self.ctx.auth.page = None
                                except:
                                    pass
                                self.ctx = None
                                self.order_manager = None
                                self._initialized = False
                except Exception:
                    # Any error means we should reset
                    self.ctx = None
                    self.order_manager = None
                    self._initialized = False
            
            # Only create new context if we don't have one or it's invalid
            if not hasattr(self, 'ctx') or self.ctx is None:
                # CRITICAL: Get per-user session path from session manager
                user_session_path = self.session_manager.get_auth_state_path(self.username)
                
                import logging
                logging.debug(f"Using session storage for user '{self.username}': {user_session_path}")
                
                # Create or load session for this user
                if not self.session_manager.session_exists(self.username):
                    logging.debug(f"Creating new Blinkit session for user: {self.username}")
                    self.session_manager.create_session(self.username)
                else:
                    if self.session_manager.is_session_valid(self.username):
                        logging.debug(f"Reusing existing Blinkit session for user: {self.username}")
                        self.session_manager.update_session_activity(self.username)
                    else:
                        logging.debug(f"Session expired for user: {self.username}. Will require re-login.")
                        self.session_manager.clear_session(self.username)
                        self.session_manager.create_session(self.username)
                
                # Create context with per-user session path
                self.ctx = BlinkitContext(session_path=user_session_path)
            
            # Ensure browser is started (this will reuse existing session if available)
            await self.ctx.ensure_started()
            
            # Verify page is properly initialized
            if not self.ctx.auth.page:
                raise RuntimeError("Browser page not initialized after ensure_started()")
            
            # Additional validation - ensure page is not None and is usable
            if self.ctx.auth.page is None:
                raise RuntimeError("Browser page is None after ensure_started()")
            
            try:
                # Verify page is actually responsive
                is_closed = self.ctx.auth.page.is_closed()
                if is_closed:
                    raise RuntimeError("Browser page is closed after ensure_started()")
            except AttributeError as e:
                raise RuntimeError(f"Browser page is invalid: {e}")
            
            self.order_manager = self.ctx.order
            self._initialized = True
            # Show concise status message
            print("Progress: Initializing Blinkit... OK: Ready")
        except Exception as e:
            log_error("Blinkit initialization", e)
            # Reset state on failure
            self._initialized = False
            self.ctx = None
            self.order_manager = None
            raise
    
    async def _validate_page(self) -> tuple[bool, Optional[str]]:
        """
        Validate that page exists and is usable.
        Returns: (is_valid, error_message)
        """
        # Use lock to ensure atomic validation
        async with self._page_lock:
            try:
                if not self.ctx or not self.ctx.auth:
                    _metrics.increment_validation_failure()
                    return (False, "Context or auth not initialized")
                
                # Critical fix: Check if page is None before accessing it
                if self.ctx.auth.page is None:
                    _metrics.increment_validation_failure()
                    return (False, "Page is None")
                
                # All checks in single try-except block to prevent race conditions
                try:
                    # Check if page is closed (only if page is not None)
                    try:
                        is_closed = self.ctx.auth.page.is_closed()
                        if is_closed:
                            _metrics.increment_validation_failure()
                            return (False, "Page is closed")
                    except AttributeError as e:
                        # Page became None during the check
                        _metrics.increment_validation_failure()
                        return (False, f"Page became None: {e}")
                    
                    # Try a simple operation to verify page is responsive
                    # This is done in the same try block to prevent page closing between checks
                    try:
                        await self.ctx.auth.page.evaluate("() => true")
                    except AttributeError as e:
                        # Page became None during evaluate
                        _metrics.increment_validation_failure()
                        return (False, f"Page became None during evaluation: {e}")
                    
                    # Update health check
                    _metrics.update_health_check()
                    return (True, None)
                    
                except (RuntimeError, AttributeError) as e:
                    # Event loop is closed or page is invalid
                    error_str = str(e).lower()
                    if "event loop is closed" in error_str or "closed" in error_str or "invalid" in error_str or "nonetype" in error_str:
                        # Mark service as needing reset
                        self._initialized = False
                        _metrics.increment_validation_failure()
                        return (False, f"Event loop or page invalid: {e}")
                    raise
                    
            except (RuntimeError, AttributeError) as e:
                # Event loop is closed or page is invalid
                error_str = str(e).lower()
                if "event loop is closed" in error_str or "closed" in error_str or "invalid" in error_str or "nonetype" in error_str:
                    self._initialized = False
                    _metrics.increment_validation_failure()
                    return (False, f"Event loop or page invalid: {e}")
                _metrics.increment_validation_failure()
                return (False, f"Validation error: {e}")
            except Exception as e:
                _metrics.increment_validation_failure()
                return (False, f"Unexpected error: {e}")
    
    async def check_login_status(self) -> bool:
        """Check if user is logged into Blinkit."""
        if not self._initialized:
            try:
                await self.initialize()
            except Exception as e:
                log_error("initializing for login check", e)
                return False
        
        # Validate page first
        is_valid, error_msg = await self._validate_page()
        if not is_valid:
            print(f"Page validation failed: {error_msg}. Reinitializing...")
            self._initialized = False
            try:
                await self.initialize()
            except Exception as e:
                log_error("reinitializing after validation failure", e)
                return False
        
        # Check again after reinitialization
        is_valid, error_msg = await self._validate_page()
        if not is_valid:
            # Still not valid after reinitialization - return False instead of raising
            log_error("page validation after reinitialization", 
                     PageValidationError(f"Page validation failed after reinitialization: {error_msg}"))
            return False
        
        try:
            return await self.ctx.auth.is_logged_in()
        except Exception as e:
            log_error("checking login status", e)
            return False
    
    def rank_product_variants(
        self,
        products: List[Dict], 
        item_name: str,
        user_preferences: Dict = None,
        consumption_patterns: Dict = None
    ) -> List[Dict]:
        """
        Rank product variants by suitability using multi-factor scoring.
        Phase 10: Intelligent product selection.
        
        Args:
            products: List of product dicts from get_search_results()
            item_name: Original item name from grocery list
            user_preferences: Dict with purchase history, brand preferences
            consumption_patterns: Dict with consumption data for this item
        
        Returns:
            Sorted list of products (highest score first) with scores
        """
        if not products:
            return []
        
        # STEP 1: Hard filter - Remove unavailable products
        available_products = [p for p in products if p.get('is_available', False)]
        
        if not available_products:
            print(f"WARNING: No available products found for '{item_name}'")
            return []
        
        print(f"Filtered to {len(available_products)}/{len(products)} available products")
        
        # STEP 2: Score each product
        scored_products = []
        
        for product in available_products:
            score = 0.0
            score_breakdown = {}
            
            # Factor 1: Name similarity (baseline relevance)
            name_lower = product['name'].lower()
            item_lower = item_name.lower()
            
            if item_lower in name_lower:
                similarity_score = 10.0
            else:
                # Simple word overlap
                item_words = set(item_lower.split())
                name_words = set(name_lower.split())
                overlap = len(item_words & name_words)
                similarity_score = overlap * 2.0
            
            score += similarity_score
            score_breakdown['name_similarity'] = similarity_score
            
            # Factor 2: Purchase history (if available)
            if user_preferences and 'purchase_history' in user_preferences:
                product_id = product.get('id')
                product_name = product.get('name', '').lower()
                
                # Check if this exact product was purchased before
                if product_id in user_preferences['purchase_history']:
                    history_score = 15.0  # Strong preference for exact match
                    score += history_score
                    score_breakdown['purchase_history'] = history_score
                # Check if brand/product name is familiar
                elif any(product_name in prev.lower() 
                        for prev in user_preferences['purchase_history'].values()):
                    brand_score = 8.0
                    score += brand_score
                    score_breakdown['brand_familiarity'] = brand_score
            
            # Factor 3: Pack size appropriateness
            if consumption_patterns and product.get('pack_size'):
                avg_consumption = consumption_patterns.get('average_quantity', 1.0)
                pack_size = product['pack_size']
                
                # Prefer pack sizes close to average consumption
                size_diff = abs(pack_size - avg_consumption)
                if size_diff < avg_consumption * 0.2:  # Within 20%
                    size_score = 5.0
                elif size_diff < avg_consumption * 0.5:  # Within 50%
                    size_score = 3.0
                else:
                    size_score = 1.0
                
                score += size_score
                score_breakdown['pack_size'] = size_score
            
            # Factor 4: Price optimization
            if product.get('price_numeric'):
                # Lower price gets higher score (but not dominant factor)
                all_prices = [p.get('price_numeric', 999999) 
                             for p in available_products 
                             if p.get('price_numeric')]
                if all_prices:
                    min_price = min(all_prices)
                    max_price = max(all_prices)
                    if max_price > min_price:
                        # Inverse scoring: cheaper = higher score
                        price_ratio = (max_price - product['price_numeric']) / (max_price - min_price)
                        price_score = price_ratio * 5.0
                    else:
                        price_score = 2.5
                    
                    score += price_score
                    score_breakdown['price_value'] = price_score
            
            # Factor 5: Active offers/discounts
            if product.get('has_offer'):
                offer_score = 3.0
                score += offer_score
                score_breakdown['has_offer'] = offer_score
            
            # Factor 6: Position in search results (mild preference for top results)
            position_score = max(0, 3.0 - (product['index'] * 0.5))
            score += position_score
            score_breakdown['search_position'] = position_score
            
            scored_products.append({
                **product,
                'total_score': score,
                'score_breakdown': score_breakdown
            })
        
        # STEP 3: Sort by score (descending)
        scored_products.sort(key=lambda p: p['total_score'], reverse=True)
        
        # Log top 3 for debugging
        print(f"\nTop 3 ranked products for '{item_name}':")
        for i, p in enumerate(scored_products[:3], 1):
            print(f"  {i}. {p['name']} - {p['price']} (Score: {p['total_score']:.1f})")
        
        return scored_products
    
    async def search_and_add_item(
        self, 
        item_name: str, 
        quantity: int = 1,
        user_preferences: Dict = None,
        consumption_patterns: Dict = None
    ) -> Dict[str, any]:
        """
        Enhanced: Search and add with intelligent product selection.
        Phase 10: Uses ranking and fallback mechanism.
        
        Args:
            item_name: Product to search for
            quantity: Quantity to add
            user_preferences: User purchase history and preferences
            consumption_patterns: Consumption data for this item
        
        Returns:
            Dict with 'success', 'product_id', 'product_name', 'message'
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Search for the product
            print(f"\nSearching for: {item_name}...")
            await self.order_manager.search_product(item_name)
            
            # Get search results (enhanced with metadata)
            results = await self.order_manager.get_search_results(limit=10)
            
            if not results:
                return {
                    'success': False,
                    'product_id': None,
                    'product_name': item_name,
                    'message': f"No results found for '{item_name}'"
                }
            
            # PHASE 10: Rank products by suitability
            ranked_products = self.rank_product_variants(
                results,
                item_name,
                user_preferences,
                consumption_patterns
            )
            
            if not ranked_products:
                return {
                    'success': False,
                    'product_id': None,
                    'product_name': item_name,
                    'message': f"No available products found for '{item_name}'"
                }
            
            # PHASE 10: Try products in ranked order (graceful fallback)
            last_error = None
            
            for attempt_num, product in enumerate(ranked_products, 1):
                product_id = product.get('id')
                product_name = product.get('name', item_name)
                price = product.get('price', 'Unknown')
                
                try:
                    if attempt_num == 1:
                        print(f"Selected: {product_name} - {price}")
                    else:
                        print(f"Trying alternative {attempt_num}: {product_name} - {price}")
                    
                    # Add to cart
                    print(f"Adding {quantity} x {product_name} to cart...")
                    await self.order_manager.add_to_cart(product_id, quantity)
                    
                    # Success!
                    return {
                        'success': True,
                        'product_id': product_id,
                        'product_name': product_name,
                        'price': price,
                        'quantity': quantity,
                        'message': f"Successfully added {quantity} x {product_name} to cart",
                        'attempt_number': attempt_num,
                        'total_score': product.get('total_score', 0)
                    }
                    
                except Exception as cart_error:
                    last_error = cart_error
                    print(f"WARNING: Failed to add {product_name}: {str(cart_error)}")
                    
                    # Continue to next product
                    if attempt_num < len(ranked_products):
                        print("Trying next best alternative...")
                        continue
                    else:
                        # All attempts failed
                        break
            
            # All products failed
            return {
                'success': False,
                'product_id': None,
                'product_name': item_name,
                'message': f"Failed to add '{item_name}' after trying {len(ranked_products)} alternatives. Last error: {str(last_error)}"
            }
            
        except Exception as e:
            log_error(f"searching/adding item {item_name}", e)
            return {
                'success': False,
                'product_id': None,
                'product_name': item_name,
                'message': f"Error processing '{item_name}': {str(e)}"
            }
    
    
    async def process_grocery_list(self, grocery_items: List[Dict]) -> Dict[str, any]:
        """
        Process a complete grocery list and add all items to Blinkit cart.
        
        Args:
            grocery_items: List of items with 'name', 'quantity', etc.
        
        Returns:
            Dict with 'success', 'added_count', 'failed_items', 'summary'
        """
        if not self._initialized:
            await self.initialize()
        
        results = {
            'success': True,
            'added_count': 0,
            'failed_items': [],
            'summary': []
        }
        
        print(f"\n Processing {len(grocery_items)} items from grocery list...")
        print("=" * 60)
        
        for i, item in enumerate(grocery_items, 1):
            # Support both 'item_name' (from AI preprocessing) and 'name' (from raw lists)
            item_name = item.get('item_name') or item.get('name', 'Unknown')
            quantity_value = item.get('quantity', '1')
            
            # Extract quantity (try to parse number from string or use direct value)
            try:
                # If already a number, use it
                if isinstance(quantity_value, (int, float)):
                    quantity = int(quantity_value)
                else:
                    # Try to extract number from quantity string
                    import re
                    qty_match = re.search(r'(\d+)', str(quantity_value))
                    quantity = int(qty_match.group(1)) if qty_match else 1
            except:
                quantity = 1
            
            print(f"\n[{i}/{len(grocery_items)}] Processing: {item_name} (Qty: {quantity})")
            
            result = await self.search_and_add_item(item_name, quantity)
            
            if result['success']:
                results['added_count'] += 1
                results['summary'].append({
                    'item': item_name,
                    'status': 'added',
                    'blinkit_name': result.get('product_name'),
                    'price': result.get('price')
                })
            else:
                results['failed_items'].append({
                    'item': item_name,
                    'reason': result.get('message', 'Unknown error')
                })
                results['summary'].append({
                    'item': item_name,
                    'status': 'failed',
                    'reason': result.get('message')
                })
            
            # Small delay between items to avoid overwhelming the API
            await asyncio.sleep(1)
        
        results['success'] = len(results['failed_items']) == 0
        return results
    
    async def proceed_to_checkout(self) -> Dict[str, any]:
        """
        Complete checkout flow with address and payment selection.
        Phase 8: Extended checkout implementation.
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Check cart contents
            cart_content = await self.order_manager.get_cart_items()
            
            if not cart_content:
                return {
                    'success': False,
                    'message': 'Cart is empty. Please add items before checkout.'
                }
            
            # Start checkout flow
            print("\n" + "="*60)
            print("STARTING CHECKOUT")
            print("="*60)
            
            # Call the complete checkout flow
            result = await self.complete_checkout_flow()
            
            return result
            
        except KeyboardInterrupt:
            print("\n\nINFO: Checkout cancelled by user")
            return {
                'success': False,
                'message': 'Checkout cancelled by user'
            }
        except Exception as e:
            log_error("proceeding to checkout", e)
            return {
                'success': False,
                'message': f"Error during checkout: {str(e)}"
            }
    
    async def complete_checkout_flow(self) -> Dict[str, any]:
        """
        Orchestrate complete checkout: address selection, payment selection, order placement.
        """
        try:
            # Step 1: Place order (navigate to checkout page)
            print("\nProgress: Initiating checkout...")
            await self.order_manager.place_order()
            
            # Step 2: Get and select delivery address
            print("\n" + "-"*60)
            print("DELIVERY ADDRESS SELECTION")
            print("-"*60)
            
            addresses = await self.order_manager.get_saved_addresses()
            
            if not addresses:
                print("\nERROR: No saved addresses found")
                print("Please add a delivery address on Blinkit website first")
                return {
                    'success': False,
                    'message': 'No saved addresses available'
                }
            
            # Display addresses
            print(f"\nFound {len(addresses)} saved address(es):\n")
            for i, addr in enumerate(addresses, 1):
                print(f"{i}. {addr}")
            
            # Get user selection
            while True:
                try:
                    choice = input(f"\nSelect address (1-{len(addresses)}): ").strip()
                    addr_index = int(choice)
                    
                    if 1 <= addr_index <= len(addresses):
                        break
                    else:
                        print(f"ERROR: Please enter a number between 1 and {len(addresses)}")
                except ValueError:
                    print("ERROR: Please enter a valid number")
                except KeyboardInterrupt:
                    raise
            
            # Select address
            print(f"\nProgress: Selecting address {addr_index}...")
            await self.order_manager.select_address(addr_index - 1)  # 0-indexed
            print(f"OK: Address selected - {addresses[addr_index - 1]}")
            
            # Step 3: Click "Proceed to Pay" button
            print(f"\nProgress: Proceeding to payment...")
            try:
                # Wait a moment for address selection to complete
                await self.page.wait_for_timeout(1000)
                
                # Look for "Proceed to Pay" or similar button
                proceed_button = self.page.locator("button:has-text('Proceed'), button:has-text('Pay')")
                if await proceed_button.count() > 0:
                    await proceed_button.first.click()
                    await self.page.wait_for_timeout(2000)  # Wait for payment options to load
                    print("OK: Proceeded to payment selection")
                else:
                    print("INFO: No proceed button found, payment options may already be visible")
            except Exception as e:
                print(f"WARNING: Could not click proceed button: {e}")
                print("Continuing to payment selection...")
            
            # Step 4: Get and select payment method (UPI IDs now visible)
            print("\n" + "-"*60)
            print("PAYMENT METHOD SELECTION")
            print("-"*60)
            
            upi_ids = await self.order_manager.get_upi_ids()
            
            if not upi_ids:
                print("\nERROR: No saved payment methods found")
                print("Please add a UPI ID on Blinkit website first")
                return {
                    'success': False,
                    'message': 'No saved payment methods available'
                }
            
            # Display UPI IDs (masked for security)
            print(f"\nFound {len(upi_ids)} saved payment method(s):\n")
            for i, upi in enumerate(upi_ids, 1):
                # Mask UPI ID for security (show first 3 and last 3 chars)
                if len(upi) > 10:
                    masked = f"{upi[:3]}***{upi[-3:]}"
                else:
                    masked = f"{upi[0]}***{upi[-1]}" if len(upi) > 2 else "***"
                print(f"{i}. {masked}")
            
            # Get user selection
            while True:
                try:
                    choice = input(f"\nSelect payment method (1-{len(upi_ids)}): ").strip()
                    upi_index = int(choice)
                    
                    if 1 <= upi_index <= len(upi_ids):
                        break
                    else:
                        print(f"ERROR: Please enter a number between 1 and {len(upi_ids)}")
                except ValueError:
                    print("ERROR: Please enter a valid number")
                except KeyboardInterrupt:
                    raise
            
            # Select UPI ID
            selected_upi = upi_ids[upi_index - 1]
            print(f"\nProgress: Selecting payment method...")
            await self.order_manager.select_upi_id(selected_upi)
            
            # Mask for display
            if len(selected_upi) > 10:
                masked_upi = f"{selected_upi[:3]}***{selected_upi[-3:]}"
            else:
                masked_upi = f"{selected_upi[0]}***{selected_upi[-1]}" if len(selected_upi) > 2 else "***"
            print(f"OK: Payment method selected - {masked_upi}")
            
            # Step 5: Click Pay Now
            print("\n" + "-"*60)
            print("PLACING ORDER")
            print("-"*60)
            
            print("\nProgress: Finalizing order...")
            await self.order_manager.click_pay_now()
            
            # Success!
            print("\n" + "="*60)
            print("ORDER PLACED SUCCESSFULLY")
            print("="*60)
            print("\nOK: Your order has been placed!")
            print("INFO: Please complete payment on the Blinkit page")
            
            return {
                'success': True,
                'message': 'Order placed successfully. Please complete payment on Blinkit.',
                'address': addresses[addr_index - 1],
                'payment_method': masked_upi
            }
            
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log_error("complete checkout flow", e)
            
            # Check for specific error conditions
            error_msg = str(e).lower()
            
            if "store" in error_msg and ("closed" in error_msg or "unavailable" in error_msg):
                print("\nERROR: Store is currently closed or unavailable")
                print("Please try again during store hours")
                return {
                    'success': False,
                    'message': 'Store is currently closed or unavailable'
                }
            
            return {
                'success': False,
                'message': f"Checkout failed: {str(e)}"
            }
    
    async def get_cart_summary(self) -> str:
        """Get a summary of current cart contents."""
        if not self._initialized:
            await self.initialize()
        
        try:
            cart_content = await self.order_manager.get_cart_items()
            return str(cart_content)
        except Exception as e:
            log_error("getting cart summary", e)
            return "Unable to retrieve cart contents."
    
    async def close(self):
        """Close the Blinkit browser session."""
        if self.ctx and self.ctx.auth:
            try:
                await self.ctx.auth.close()
            except Exception as e:
                # If it's a 'NoneType' error during close, it means browser is already gone
                if "'NoneType' object has no attribute 'send'" in str(e):
                    pass
                else:
                    log_error("closing Blinkit session", e)


async def verify_cart_contents(service, added_items: List[str] = None, max_retries: int = 3) -> Dict[str, any]:
    """
    Verify that cart actually has items after adding with retry logic.
    
    Args:
        service: BlinkitIntegrationService instance
        added_items: List of product names that were added
        max_retries: Maximum retry attempts
    
    Returns:
        Dict with has_items, item_count, verified_items, and success status
    """
    start_time = time.time()
    
    try:
        print("\n Verifying cart contents...")
        page = service.order_manager.page
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    print(f"Retry attempt {attempt + 1}/{max_retries}...")
                    _metrics.increment_retry()
                    await asyncio.sleep(2)  # 2 second delay between retries
                
                # Wait for network to be idle
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except:
                    pass  # Continue even if network idle times out
                
                # Strategy 1: Check for Bill details text
                has_bill_details = await page.is_visible("text=/Bill details/i")
                
                # Strategy 2: Check for cart item containers
                has_cart_items = await page.is_visible("div[class*='CartItem__Container']")
                
                # Strategy 3: Check cart count badge
                cart_count = 0
                try:
                    cart_badge = page.locator("div[class*='CartButton'] span, div[class*='cart'] span")
                    if await cart_badge.count() > 0:
                        badge_text = await cart_badge.first.inner_text()
                        import re
                        count_match = re.search(r'(\d+)', badge_text)
                        if count_match:
                            cart_count = int(count_match.group(1))
                except:
                    pass
                
                # Strategy 4: Verify product names if provided
                verified_items = []
                if added_items and (has_bill_details or has_cart_items):
                    try:
                        # Try to open cart if not already open
                        if not has_bill_details:
                            await service.order_manager.get_cart_items()
                            await page.wait_for_timeout(2000)
                        
                        # Get all product names in cart
                        product_names = page.locator("div[class*='Product__Title'], div[class*='CartItem'] div[class*='name']")
                        count = await product_names.count()
                        
                        for i in range(count):
                            try:
                                name = await product_names.nth(i).inner_text()
                                # Check if any added item matches (partial match)
                                for added_item in added_items:
                                    if added_item.lower() in name.lower() or name.lower() in added_item.lower():
                                        verified_items.append(name)
                                        break
                            except:
                                pass
                    except Exception as e:
                        print(f"Could not verify product names: {e}")
                
                # Determine if cart has items
                has_items = has_bill_details or has_cart_items or cart_count > 0 or len(verified_items) > 0
                
                if has_items:
                    duration = time.time() - start_time
                    _metrics.record_cart_verification(duration)
                    
                    print(f"Cart verification successful: {cart_count or len(verified_items)} items")
                    if verified_items:
                        print(f"Verified items: {', '.join(verified_items[:3])}{'...' if len(verified_items) > 3 else ''}")
                    
                    return {
                        'success': True,
                        'has_items': True,
                        'item_count': cart_count or len(verified_items),
                        'verified_items': verified_items,
                        'attempt': attempt + 1
                    }
                
                # If last attempt, fail
                if attempt == max_retries - 1:
                    duration = time.time() - start_time
                    _metrics.record_cart_verification(duration)
                    
                    error_msg = "Cart appears empty after all retry attempts"
                    print(f"{error_msg}")
                    raise CartVerificationError(error_msg)
                    
            except CartVerificationError:
                raise  # Re-raise cart verification errors
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                print(f"Verification attempt {attempt + 1} failed: {e}")
        
        # Should not reach here
        raise CartVerificationError("Cart verification failed")
        
    except CartVerificationError:
        raise
    except Exception as e:
        duration = time.time() - start_time
        _metrics.record_cart_verification(duration)
        
        error_msg = f"Error during cart verification: {e}"
        print(f"{error_msg}")
        raise CartVerificationError(error_msg) from e



# Per-user service instances (CRITICAL: prevents session sharing)
_user_services: Dict[str, BlinkitIntegrationService] = {}
_service_lock = threading.Lock()


def get_blinkit_service(username: str) -> BlinkitIntegrationService:
    """
    Get or create Blinkit integration service for a specific user.
    
    CRITICAL: Each user gets their own service instance to prevent session sharing.
    
    Args:
        username: Smart Fridge username
        
    Returns:
        BlinkitIntegrationService instance for the specified user
        
    Raises:
        ValueError: If username is empty or None
    """
    if not username:
        raise ValueError("Username is required to get Blinkit service")
    
    with _service_lock:
        # Check if we already have a service for this user
        if username in _user_services:
            service = _user_services[username]
            if service._initialized:
                print(f"Reusing existing Blinkit service for user: {username}")
                return service
            else:
                # Service exists but not initialized - remove it
                print(f"Removing uninitialized service for user: {username}")
                del _user_services[username]
        
        # Create new service for this user
        print(f" Creating new Blinkit service for user: {username}")
        service = BlinkitIntegrationService(username=username)
        _user_services[username] = service
        
        return service


async def clear_user_blinkit_service(username: str) -> bool:
    """
    Clear/remove Blinkit service for a specific user and close session.
    
    Call this when user logs out of Smart Fridge.
    
    Args:
        username: Smart Fridge username
        
    Returns:
        True if service was cleared, False if no service existed
    """
    with _service_lock:
        if username in _user_services:
            print(f"Clearing Blinkit service for user: {username}")
            service = _user_services.pop(username)
            try:
                if service._initialized:
                    await service.close()
            except Exception as e:
                log_error(f"error closing service for {username}", e)
            return True
        return False


def get_active_blinkit_users() -> List[str]:
    """
    Get list of users with active Blinkit services.
    
    Returns:
        List of usernames
    """
    with _service_lock:
        return list(_user_services.keys())


def return_service_to_pool(service: BlinkitIntegrationService):
    """
    Legacy function - no longer needed with per-user services.
    
    Kept for backward compatibility.
    """
    # No-op: per-user services are managed in _user_services dict
    pass


async def preprocess_grocery_list_with_ai(grocery_items: List[Dict], vision_service) -> Dict[str, any]:
    """
    Preprocess grocery list using Gemini Flash for normalization.
    Phase 9: AI preprocessing pipeline.
    
    Args:
        grocery_items: Raw grocery list items
        vision_service: VisionService instance with Gemini Flash
    
    Returns:
        Dict with 'success', 'normalized_items', and 'message'
    """
    try:
        print("\nNormalizing grocery list with AI...")
        
        # Step 1: Format raw items for AI input
        raw_items_text = "\n".join([
            f"- {item.get('name', 'Unknown')} "
            f"({item.get('quantity', '1')} {item.get('category', '')})"
            for item in grocery_items
        ])
        
        # Step 2: Create normalization prompt
        normalization_prompt = f"""You are a grocery list normalizer. Convert the following grocery items into a strict JSON format.

Rules:
1. Normalize item names (no slang, no ambiguity, use common product names)
2. Standardize quantities and units (use metric: gram, kilogram, liter, milliliter, piece)
3. Remove duplicates (combine quantities if same item appears multiple times)
4. Split combined items into separate atomic items
5. Return ONLY valid JSON array, no explanations, no prose, no markdown

Input grocery list:
{raw_items_text}

Output format (strict JSON only):
[
  {{"item_name": "milk", "quantity": 2, "unit": "liter"}},
  {{"item_name": "tomato", "quantity": 500, "unit": "gram"}}
]

Return only the JSON array:"""
        
        # Step 3: Call Gemini Flash
        if not vision_service or not vision_service.text_model:
            return {
                'success': False,
                'message': 'AI service not available',
                'normalized_items': []
            }
        
        generation_config = {
            'temperature': 0.1,  # Low temperature for deterministic output
            'top_p': 0.9,
            'top_k': 20,
            'max_output_tokens': 2048,
        }
        
        response = vision_service.text_model.generate_content(
            normalization_prompt,
            generation_config=generation_config
        )
        
        response_text = vision_service._extract_text_from_response(response)
        
        if not response_text:
            return {
                'success': False,
                'message': 'AI returned empty response',
                'normalized_items': []
            }
        
        # Step 4: Extract JSON from response
        # Remove markdown code blocks if present
        json_text = response_text.strip()
        if json_text.startswith('```'):
            # Extract content between code fences
            lines = json_text.split('\n')
            json_lines = []
            in_code_block = False
            for line in lines:
                if line.startswith('```'):
                    in_code_block = not in_code_block
                    continue
                if in_code_block or (not line.startswith('```')):
                    json_lines.append(line)
            json_text = '\n'.join(json_lines).strip()
        
        # Step 5: Parse and validate JSON
        import json
        try:
            normalized_items = json.loads(json_text)
        except json.JSONDecodeError as e:
            print(f"ERROR: AI returned invalid JSON: {e}")
            print(f"Response: {response_text[:200]}...")
            return {
                'success': False,
                'message': f'AI returned invalid JSON: {str(e)}',
                'normalized_items': []
            }
        
        # Step 6: Validate structure
        if not isinstance(normalized_items, list):
            return {
                'success': False,
                'message': 'AI output is not a list',
                'normalized_items': []
            }
        
        if len(normalized_items) == 0:
            return {
                'success': False,
                'message': 'AI returned empty list',
                'normalized_items': []
            }
        
        # Validate each item has required fields
        validated_items = []
        for item in normalized_items:
            if not isinstance(item, dict):
                continue
            
            item_name = item.get('item_name', '').strip()
            quantity = item.get('quantity')
            unit = item.get('unit', '').strip()
            
            if not item_name:
                continue
            
            # Ensure quantity is numeric
            try:
                quantity = float(quantity) if quantity else 1.0
            except (ValueError, TypeError):
                quantity = 1.0
            
            validated_items.append({
                'name': item_name,
                'quantity': quantity,
                'unit': unit,
                'category': 'Groceries',
                'type': 'normalized'
            })
        
        if len(validated_items) == 0:
            return {
                'success': False,
                'message': 'No valid items after validation',
                'normalized_items': []
            }
        
        print(f"OK: Normalized {len(grocery_items)} items to {len(validated_items)} items")
        
        return {
            'success': True,
            'normalized_items': validated_items,
            'message': f'Successfully normalized {len(validated_items)} items',
            'original_count': len(grocery_items),
            'normalized_count': len(validated_items)
        }
        
    except Exception as e:
        log_error("AI grocery list preprocessing", e)
        return {
            'success': False,
            'message': f'Preprocessing error: {str(e)}',
            'normalized_items': []
        }



async def order_grocery_list_async(grocery_items: List[Dict], auto_checkout: bool = True, username: str = None) -> Dict[str, any]:
    """
    Async function to order items from a grocery list.
    Now user-aware for session isolation.
    
    Args:
        grocery_items: List of grocery items with 'name', 'quantity', etc.
        auto_checkout: Whether to automatically proceed to checkout
        username: Smart Fridge username
    
    Returns:
        Dict with ordering results
    """
    if not username:
        return {"success": False, "message": "Username is required for ordering."}

    service = get_blinkit_service(username=username)
    
    try:
        # Always reinitialize to ensure fresh state
        if not service._initialized:
            await service.initialize()
        else:
            # Verify existing state is valid
            is_valid, error_msg = await service._validate_page()
            if not is_valid:
                print(f"Existing session invalid ({error_msg}). Reinitializing...")
                # Reset service state completely to ensure fresh browser context
                service._initialized = False
                service.ctx = None
                service.order_manager = None
                await service.initialize()
        
        # Double-check after initialization
        is_valid, error_msg = await service._validate_page()
        if not is_valid:
            # If validation still fails, the event loop might be closed
            # Reset everything and try one more time
            print(f"Page validation failed ({error_msg}). Resetting service...")
            service._initialized = False
            service.ctx = None
            service.order_manager = None
            await service.initialize()
            
            is_valid, error_msg = await service._validate_page()
            if not is_valid:
                return {
                    "success": False,
                    "message": f"Failed to initialize browser: {error_msg}. Please try option 13 to login first.",
                    "requires_login": True
                }
        
        is_logged_in = await service.check_login_status()
        if not is_logged_in:
            return {
                "success": False,
                "message": "Not logged into Blinkit. Please login first using option 13 (Blinkit Integration & Ordering) from the main menu.",
                "requires_login": True
            }
        
        # PHASE 9: AI Preprocessing
        print("\n" + "="*60)
        print("PREPROCESSING GROCERY LIST")
        print("="*60)
        
        # Get VisionService instance
        try:
            from src.services.vision import VisionService
            vision_service = VisionService()
        except Exception as e:
            print(f"WARNING: Could not load AI service: {e}")
            print("Proceeding with raw grocery list...")
            vision_service = None
        
        # Preprocess if AI is available
        if vision_service and vision_service.text_model:
            preprocessing_result = await preprocess_grocery_list_with_ai(
                grocery_items, 
                vision_service
            )
            
            if preprocessing_result['success']:
                # Use normalized items
                grocery_items = preprocessing_result['normalized_items']
                print(f"\nUsing {len(grocery_items)} normalized items")
                
                # Show what AI normalized (for transparency)
                print("\nAI Normalization Results:")
                for item in grocery_items:
                    print(f"  - {item.get('item_name', 'unknown')}: {item.get('quantity', '?')} {item.get('unit', '?')}")
            else:
                # Fall back to raw items with warning
                print(f"\nWARNING: Preprocessing failed: {preprocessing_result['message']}")
                print("Proceeding with original grocery list...")
        else:
            print("\nWARNING: AI service not available")
            print("Proceeding with original grocery list...")

        
        # Process grocery list
        process_result = await service.process_grocery_list(grocery_items)
        
        if process_result['added_count'] == 0:
            return {
                'success': False,
                'message': 'No items were successfully added to cart.',
                'details': process_result
            }
        
        print("\n" + "="*60)
        print("VERIFYING CART")
        print("="*60)

        # Collect added item names for verification
        added_item_names = [item.get('name') for item in grocery_items if item.get('name')]
        
        try:
            cart_verification = await verify_cart_contents(service, added_items=added_item_names, max_retries=3)
            
            if not cart_verification.get('has_items'):
                print("\n WARNING: Cart verification failed!")
                print("Items may not have been added successfully.")
                print("Trying to navigate to cart to check...")
                
                # Critical Fix - Navigate to Cart Page After Adding Items
                print("\n Navigating to cart page...")
                await service.order_manager.page.goto("https://blinkit.com/cart", wait_until="domcontentloaded")
                await service.order_manager.page.wait_for_timeout(3000)
                
                # Now check if cart has items
                if await service.order_manager.page.is_visible("text=Your cart is empty"):
                    print("Cart is empty! Items were not added successfully.")
                    return {
                        'success': False,
                        'message': 'Cart is empty - items not added',
                        'added_count': 0,
                        'total_items': len(grocery_items)
                    }
        except CartVerificationError as e:
            print(f"\n Cart verification error: {e}")
            # Continue anyway - items might still be in cart

        # Proceed to checkout if requested
        checkout_result = None
        if auto_checkout:
            checkout_result = await service.proceed_to_checkout()
        
        # Build summary message
        summary_lines = [
            f"\n Successfully added {process_result['added_count']}/{len(grocery_items)} items to cart"
        ]
        
        if process_result['failed_items']:
            summary_lines.append(f"\n Failed items ({len(process_result['failed_items'])}):")
            for failed in process_result['failed_items']:
                summary_lines.append(f"  - {failed['item']}: {failed['reason']}")
        
        if checkout_result and checkout_result.get('success'):
            summary_lines.append("\n Cart is ready for payment!")
            summary_lines.append(" Please complete payment on Blinkit website/app")
        
        return {
            'success': True,
            'added_count': process_result['added_count'],
            'total_items': len(grocery_items),
            'failed_items': process_result['failed_items'],
            'checkout_status': checkout_result,
            'message': '\n'.join(summary_lines),
            'details': process_result
        }
        
    except Exception as e:
        log_error("ordering grocery list", e)
        return {
            'success': False,
            'message': f"Error during ordering: {str(e)}"
        }


def order_grocery_list(grocery_items: List[Dict], auto_checkout: bool = True) -> Dict[str, any]:
    """
    Synchronous wrapper for ordering grocery list.
    Runs the async function in an event loop.
    
    Note: This function is deprecated. Use order_grocery_list_async() with username parameter instead.
    """
    # Try to get existing event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If loop is already running, we need to use a different approach
            # Create a new thread with its own event loop
            import threading
            result_container = {'result': None, 'exception': None}
            
            def run_in_thread():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    result_container['result'] = new_loop.run_until_complete(
                        order_grocery_list_async(grocery_items, auto_checkout)
                    )
                except Exception as e:
                    result_container['exception'] = e
                finally:
                    # Don't close the loop immediately - let it clean up naturally
                    # Closing immediately can cause issues with Playwright cleanup
                    try:
                        new_loop.run_until_complete(asyncio.sleep(0.1))
                    except:
                        pass
                    new_loop.close()
            
            thread = threading.Thread(target=run_in_thread)
            thread.start()
            thread.join()
            
            if result_container['exception']:
                raise result_container['exception']
            return result_container['result']
        else:
            return loop.run_until_complete(order_grocery_list_async(grocery_items, auto_checkout))
    except RuntimeError:
        # No event loop, create one
        return asyncio.run(order_grocery_list_async(grocery_items, auto_checkout))


def get_service_metrics() -> Dict[str, any]:
    """Get current service metrics."""
    return _metrics.get_stats()


def reset_service_metrics():
    """Reset service metrics."""
    global _metrics
    _metrics = ServiceMetrics()

# ===== From src/integration/blinkit_ordering.py =====

_user_login_cache = {}  # {username: {"is_logged_in": bool, "checked_at": datetime}}
_CACHE_DURATION = timedelta(minutes=5)

def require_blinkit_login(func):
    """
    Decorator to check Blinkit login status before executing function.
    Now user-aware with status caching (Fix for Issue 4).
    """
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        # CRITICAL: Get username from self.user_mgr
        if not hasattr(self, 'user_mgr') or not self.user_mgr.current_user:
            return {
                "success": False,
                "requires_login": True,
                "message": "Please log into Smart Fridge first."
            }
        
        username = self.user_mgr.current_user['username']
        now = datetime.now()
        
        # Check cache first
        user_cache = _user_login_cache.get(username)
        if user_cache and user_cache.get("is_logged_in"):
            checked_at = user_cache.get("checked_at")
            if checked_at and (now - checked_at) < _CACHE_DURATION:
                # Cache hit and still valid
                return await func(self, *args, **kwargs)
        
        # Cache miss or expired - check login status
        try:
            from src.services.integrations.blinkit import get_blinkit_service
            
            # CRITICAL: Pass username for per-user session
            service = get_blinkit_service(username=username)
            
            # Initialize if needed
            if not service._initialized:
                try:
                    await service.initialize()
                except Exception as init_error:
                    return {
                        "success": False, 
                        "requires_login": True,
                        "message": f"Failed to initialize Blinkit for {username}: {str(init_error)}. Please try option 13 to login."
                    }
            
            # Check login status
            is_logged_in = await service.check_login_status()
            
            # Update cache
            _user_login_cache[username] = {
                "is_logged_in": is_logged_in,
                "checked_at": now
            }
            
            if not is_logged_in:
                return {
                    "success": False, "requires_login": True,
                    "message": f"User '{username}' not logged into Blinkit. Please login first using option 13."
                }
        except Exception as e:
            return {
                "success": False, 
                "requires_login": True,
                "error": str(e), 
                "message": f"Failed to check Blinkit login status for {username}: {str(e)}. Please try option 13."
            }
        
        return await func(self, *args, **kwargs)
    return wrapper

class BlinkitOrderingMixin:
    """Methods for ordering via Blinkit."""

    def order_from_saved_list(self):
        """Order items from a saved grocery list via Blinkit."""
        from src.services.integrations.blinkit import BLINKIT_AVAILABLE
        if not BLINKIT_AVAILABLE:
            print("\n Blinkit integration is not available.")
            return
        
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                if not self.user_mgr.current_user:
                    print("\nPlease log in to order")
                    return
                
                username = self.user_mgr.current_user['username']
                saved_lists = list(db['grocery_lists'].find({"username": username}).sort("created_at", -1).limit(10))
                
                if not saved_lists:
                    print("\n No saved lists found.")
                    return
                
                print("\n SELECT LIST TO ORDER")
                for i, gl in enumerate(saved_lists, 1):
                    print(f"{i}. {gl['name']} ({len(gl.get('items', []))} items)")
                print("0. Cancel")
                
                try:
                    choice = int(input("\nSelect list: "))
                    if choice == 0: return
                    if 1 <= choice <= len(saved_lists):
                        selected = saved_lists[choice - 1]
                        items = selected.get('items', [])
                        if not items:
                            print("Empty list.")
                            return
                        
                        print(f"\n Ordering from '{selected['name']}'...")
                        
                        # Fix: Check if event loop is already running
                        import asyncio
                        try:
                            loop = asyncio.get_running_loop()
                            # Event loop is already running, we're in async context
                            # This shouldn't happen in normal flow, but handle it
                            print("Event loop already running, using thread approach...")
                            import threading
                            result_container = {'result': None, 'exception': None}
                            
                            def run_in_thread():
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                try:
                                    result_container['result'] = new_loop.run_until_complete(
                                        self._perform_blinkit_order(items)
                                    )
                                except Exception as e:
                                    result_container['exception'] = e
                                finally:
                                    try:
                                        new_loop.run_until_complete(asyncio.sleep(0.1))
                                    except:
                                        pass
                                    new_loop.close()
                            
                            thread = threading.Thread(target=run_in_thread)
                            thread.start()
                            thread.join()
                            
                            if result_container['exception']:
                                raise result_container['exception']
                            result = result_container['result']
                        except RuntimeError:
                            # No event loop running, safe to use asyncio.run()
                            # But first, ensure any old service is cleaned up
                            from src.services.integrations.blinkit import get_blinkit_service
                            
                            # CRITICAL: Pass username for per-user session
                            service = get_blinkit_service(username=username)
                            # Reset service to ensure fresh initialization in new event loop
                            service._initialized = False
                            service.ctx = None
                            service.order_manager = None
                            
                            result = asyncio.run(self._perform_blinkit_order(items))
                        
                        if result.get('success'):
                            print(f"\n ORDERING COMPLETE: {result.get('message')}")
                        else:
                            print(f"\n ORDERING FAILED: {result.get('message')}")
                            if result.get('requires_login'):
                                print("Please login to Blinkit first.")
                except ValueError: pass
        except Exception as e:
            log_error("ordering from saved list", e)

    @require_blinkit_login
    async def _perform_blinkit_order(self, items: List[Dict]):
        """Perform order with @require_blinkit_login protection. (Fix for Issue 4)"""
        try:
            # CRITICAL: Get username for session isolation
            username = self.user_mgr.current_user['username']
            
            from src.services.integrations.blinkit import order_grocery_list_async
            result = await order_grocery_list_async(items, auto_checkout=True, username=username)
            return result
        except Exception as e:
            return {"success": False, "error": str(e), "message": f"Error during ordering: {e}"}

# ===== From src/integration/blinkit_session_manager.py =====

@dataclass
class SessionMetadata:
    """Metadata for a Blinkit session."""
    username: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    blinkit_phone_hash: Optional[str] = None  # Hashed for privacy
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'username': self.username,
            'created_at': self.created_at.isoformat(),
            'last_used_at': self.last_used_at.isoformat(),
            'expires_at': self.expires_at.isoformat(),
            'blinkit_phone_hash': self.blinkit_phone_hash
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionMetadata':
        """Create from dictionary."""
        return cls(
            username=data['username'],
            created_at=datetime.fromisoformat(data['created_at']),
            last_used_at=datetime.fromisoformat(data['last_used_at']),
            expires_at=datetime.fromisoformat(data['expires_at']),
            blinkit_phone_hash=data.get('blinkit_phone_hash')
        )
    
    def is_expired(self) -> bool:
        """Check if session has expired."""
        return datetime.now() > self.expires_at
    
    def update_last_used(self) -> None:
        """Update last used timestamp."""
        self.last_used_at = datetime.now()


class BlinkitSessionManager:
    """
    Manages per-user Blinkit sessions to ensure session isolation.
    
    Each Smart Fridge user gets their own isolated Blinkit session stored
    in a separate directory. This prevents session sharing across users.
    """
    
    def __init__(self, base_cache_dir: str = "cache", session_ttl_days: int = 7):
        """
        Initialize session manager.
        
        Args:
            base_cache_dir: Base directory for cache storage
            session_ttl_days: Session time-to-live in days (default: 7)
        """
        self.base_cache_dir = Path(base_cache_dir)
        self.sessions_dir = self.base_cache_dir / "blinkit_sessions"
        self.session_ttl = timedelta(days=session_ttl_days)
        self._lock = threading.RLock()
        
        # Create base directories
        self._ensure_directories()
    
    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        
        # Set restrictive permissions (owner read/write only)
        try:
            os.chmod(self.sessions_dir, 0o700)
        except Exception as e:
            log_error("set session directory permissions", e)
    
    def _get_user_session_dir(self, username: str) -> Path:
        """
        Get session directory for a specific user.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            Path to user's session directory
        """
        # Sanitize username for filesystem
        safe_username = "".join(c for c in username if c.isalnum() or c in ('_', '-')).lower()
        return self.sessions_dir / safe_username
    
    def _get_metadata_path(self, username: str) -> Path:
        """Get path to session metadata file."""
        return self._get_user_session_dir(username) / "session_metadata.json"
    
    def _get_auth_state_path(self, username: str) -> Path:
        """Get path to Playwright auth state file."""
        return self._get_user_session_dir(username) / "auth_state.json"
    
    def _hash_phone(self, phone: str) -> str:
        """Hash phone number for privacy (one-way hash)."""
        return hashlib.sha256(phone.encode()).hexdigest()[:16]
    
    def session_exists(self, username: str) -> bool:
        """
        Check if a session exists for the given user.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            True if session exists, False otherwise
        """
        with self._lock:
            metadata_path = self._get_metadata_path(username)
            auth_state_path = self._get_auth_state_path(username)
            return metadata_path.exists() and auth_state_path.exists()
    
    def is_session_valid(self, username: str) -> bool:
        """
        Check if session exists and is not expired.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            True if session is valid, False otherwise
        """
        with self._lock:
            if not self.session_exists(username):
                return False
            
            try:
                metadata = self.get_session_metadata(username)
                if metadata is None:
                    return False
                
                return not metadata.is_expired()
            except Exception as e:
                log_error(f"validate session for {username}", e)
                return False
    
    def get_session_metadata(self, username: str) -> Optional[SessionMetadata]:
        """
        Get session metadata for a user.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            SessionMetadata if exists, None otherwise
        """
        with self._lock:
            metadata_path = self._get_metadata_path(username)
            
            if not metadata_path.exists():
                return None
            
            try:
                with open(metadata_path, 'r') as f:
                    data = json.load(f)
                return SessionMetadata.from_dict(data)
            except Exception as e:
                log_error(f"read session metadata for {username}", e)
                return None
    
    def create_session(self, username: str, blinkit_phone: Optional[str] = None) -> SessionMetadata:
        """
        Create a new session for a user.
        
        Args:
            username: Smart Fridge username
            blinkit_phone: Blinkit phone number (optional, will be hashed)
            
        Returns:
            SessionMetadata for the new session
        """
        with self._lock:
            # Create user session directory
            session_dir = self._get_user_session_dir(username)
            session_dir.mkdir(parents=True, exist_ok=True)
            
            # Set restrictive permissions
            try:
                os.chmod(session_dir, 0o700)
            except Exception as e:
                log_error(f"set permissions for {username} session", e)
            
            # Create metadata
            now = datetime.now()
            metadata = SessionMetadata(
                username=username,
                created_at=now,
                last_used_at=now,
                expires_at=now + self.session_ttl,
                blinkit_phone_hash=self._hash_phone(blinkit_phone) if blinkit_phone else None
            )
            
            # Save metadata
            self._save_metadata(username, metadata)
            
            return metadata
    
    def _save_metadata(self, username: str, metadata: SessionMetadata) -> None:
        """Save session metadata to disk."""
        metadata_path = self._get_metadata_path(username)
        
        try:
            with open(metadata_path, 'w') as f:
                json.dump(metadata.to_dict(), f, indent=2)
            
            # Set restrictive permissions on metadata file
            os.chmod(metadata_path, 0o600)
        except Exception as e:
            log_error(f"save session metadata for {username}", e)
            raise
    
    def update_session_activity(self, username: str) -> None:
        """
        Update last activity timestamp for a session.
        
        Args:
            username: Smart Fridge username
        """
        with self._lock:
            metadata = self.get_session_metadata(username)
            if metadata:
                metadata.update_last_used()
                self._save_metadata(username, metadata)
    
    def get_auth_state_path(self, username: str) -> str:
        """
        Get path to Playwright auth state file for a user.
        
        This is the main method used by BlinkitIntegrationService to get
        the per-user storage path for Playwright.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            Absolute path to auth state file as string
        """
        return str(self._get_auth_state_path(username).absolute())
    
    def clear_session(self, username: str) -> bool:
        """
        Clear/delete session for a user.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            True if session was cleared, False if no session existed
        """
        with self._lock:
            session_dir = self._get_user_session_dir(username)
            
            if not session_dir.exists():
                return False
            
            try:
                # Remove all files in session directory
                for file_path in session_dir.iterdir():
                    if file_path.is_file():
                        file_path.unlink()
                
                # Remove directory
                session_dir.rmdir()
                
                return True
            except Exception as e:
                log_error(f"clear session for {username}", e)
                return False
    
    def cleanup_expired_sessions(self) -> int:
        """
        Clean up all expired sessions.
        
        Returns:
            Number of sessions cleaned up
        """
        with self._lock:
            cleaned = 0
            
            if not self.sessions_dir.exists():
                return 0
            
            for user_dir in self.sessions_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                
                username = user_dir.name
                
                try:
                    metadata = self.get_session_metadata(username)
                    if metadata and metadata.is_expired():
                        if self.clear_session(username):
                            cleaned += 1
                            print(f"Cleaned expired session for user: {username}")
                except Exception as e:
                    log_error(f"cleanup session for {username}", e)
            
            return cleaned
    
    def get_all_sessions(self) -> Dict[str, SessionMetadata]:
        """
        Get metadata for all existing sessions.
        
        Returns:
            Dictionary mapping username to SessionMetadata
        """
        with self._lock:
            sessions = {}
            
            if not self.sessions_dir.exists():
                return sessions
            
            for user_dir in self.sessions_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                
                username = user_dir.name
                metadata = self.get_session_metadata(username)
                
                if metadata:
                    sessions[username] = metadata
            
            return sessions
    
    def get_session_info(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Get human-readable session information.
        
        Args:
            username: Smart Fridge username
            
        Returns:
            Dictionary with session info, or None if no session
        """
        metadata = self.get_session_metadata(username)
        
        if not metadata:
            return None
        
        now = datetime.now()
        time_remaining = metadata.expires_at - now
        
        return {
            'username': metadata.username,
            'created': metadata.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_used': metadata.last_used_at.strftime('%Y-%m-%d %H:%M:%S'),
            'expires': metadata.expires_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_expired': metadata.is_expired(),
            'days_remaining': max(0, time_remaining.days),
            'hours_remaining': max(0, time_remaining.seconds // 3600),
            'has_phone': metadata.blinkit_phone_hash is not None
        }


# Global session manager instance
_session_manager: Optional[BlinkitSessionManager] = None
_manager_lock = threading.Lock()


def get_session_manager() -> BlinkitSessionManager:
    """
    Get global session manager instance (singleton).
    
    Returns:
        BlinkitSessionManager instance
    """
    global _session_manager
    
    with _manager_lock:
        if _session_manager is None:
            _session_manager = BlinkitSessionManager()
        return _session_manager

# ===== From src/integration/integration_status.py =====

try:
    from src.services.integrations.blinkit import order_grocery_list, order_grocery_list_async
    BLINKIT_AVAILABLE = True
except ImportError:
    BLINKIT_AVAILABLE = False

# ===== From src/integration/menu.py =====

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'smart_fridge'))

try:
    from src.services.integrations.blinkit import get_blinkit_service, clear_user_blinkit_service
    BLINKIT_AVAILABLE = True
except ImportError:
    BLINKIT_AVAILABLE = False
    print("Blinkit integration not available")


class _DummyInventorySync:
    """Placeholder inventory sync implementation."""

    def sync(self) -> None:
        """No-op sync method kept for backwards compatibility."""
        print("Inventory sync is not fully implemented yet.")


class _DummyOrderAutomation:
    """Placeholder order automation implementation."""

    async def close(self) -> None:  # noqa: D401
        """No-op close method kept for backwards compatibility."""
        return None


def get_inventory_sync() -> _DummyInventorySync:
    """
    Return a dummy inventory sync object.

    This keeps existing calls in `main.py` working without raising ImportError.
    """
    return _DummyInventorySync()


def get_order_automation(
    inventory_mgr: Any, user_mgr: Any, recipe_mgr: Any
) -> _DummyOrderAutomation:
    """
    Return a dummy order automation object.

    The arguments are accepted but ignored; they are present purely so the
    function matches the expected call-site signature.
    """
    _ = (inventory_mgr, user_mgr, recipe_mgr)
    return _DummyOrderAutomation()


async def _ensure_initialized(service) -> None:
    """Ensure the Blinkit service is initialized."""
    if not service._initialized:
        print("\n Initializing Blinkit service...")
        await service.initialize()
    else:
        # Even if initialized, ensure browser is still running
        if hasattr(service, 'ctx') and service.ctx:
            try:
                await service.ctx.ensure_started()
                # Verify page exists and is valid
                if not service.ctx.auth.page or service.ctx.auth.page is None:
                    # Page is None, reinitialize
                    service._initialized = False
                    print("\n Reinitializing Blinkit service (browser page is None)...")
                    await service.initialize()
                elif service.ctx.auth.page.is_closed():
                    # Page is closed, reinitialize
                    service._initialized = False
                    print("\n Reinitializing Blinkit service (browser was closed)...")
                    await service.initialize()
            except AttributeError:
                # Page became None or invalid during check
                service._initialized = False
                print("\n Reinitializing Blinkit service (page became invalid)...")
                await service.initialize()
            except Exception:
                # Browser might have closed, reinitialize
                service._initialized = False
                print("\n Reinitializing Blinkit service...")
                await service.initialize()
        else:
            # No context, initialize
            await service.initialize()


async def _verify_page_ready(service) -> bool:
    """Verify that the browser page is ready for use."""
    try:
        await _ensure_initialized(service)
        if not service.ctx or not service.ctx.auth:
            return False
        if not service.ctx.auth.page or service.ctx.auth.page is None:
            return False
        try:
            if service.ctx.auth.page.is_closed():
                # Page closed, reinitialize
                service._initialized = False
                await service.initialize()
                return service.ctx and service.ctx.auth and service.ctx.auth.page and service.ctx.auth.page is not None and not service.ctx.auth.page.is_closed()
        except AttributeError:
            # Page became None during check
            return False
        return True
    except Exception:
        return False


async def run_integration_menu(
    order_automation: Any, inventory_sync: Any, username: str = None
) -> None:
    """
    Main Blinkit integration menu with full functionality.
    
    Provides options for:
    - Login/Logout
    - Setting location
    - Searching products
    - Managing cart
    - Checkout and payment
    """
    if not BLINKIT_AVAILABLE:
        print("\n Blinkit integration is not available.")
        print("Please ensure blinkit_integration.py is properly configured.")
        return
    
    if not username:
        print("\n Error: Username is required for Blinkit integration.")
        return

    service = get_blinkit_service(username=username)
    
    # Force initialization at menu start
    try:
        # Show concise initialization message
        print("\nProgress: Connecting to Blinkit...")
        await _ensure_initialized(service)
        print("OK: Connected!")
    except Exception as e:
        print(f"\nERROR: Failed to initialize Blinkit service: {e}")
        print("Please check your browser setup and try again.")
        return
    
    print("\n" + "="*60)
    print("BLINKIT INTEGRATION MENU")
    print("="*60)
    
    while True:
        try:
            # Check login status (with error handling)
            try:
                is_logged_in = await service.check_login_status()
                login_status = " Logged In" if is_logged_in else " Not Logged In"
            except Exception as e:
                # If check fails, assume not logged in
                login_status = " Not Logged In"
                is_logged_in = False
                # Try to initialize if not already done
                if not service._initialized:
                    try:
                        await _ensure_initialized(service)
                    except Exception:
                        pass  # Will show error if user tries to use features
            
            print(f"\n Status: {login_status}")
            print("\n MENU OPTIONS:")
            print("1. Login to Blinkit")
            print("2. Check Login Status")
            print("3. Set Delivery Location")
            print("4. Search Products")
            print("5. View Cart")
            print("6. Add Item to Cart")
            print("7. Proceed to Checkout")
            print("8. View Saved Addresses")
            print("9. Select Payment Method (UPI)")
            print("10. Pay Now")
            print("11. Logout from Blinkit")
            print("0. Back to Main Menu")
            
            choice = input("\nEnter your choice: ").strip()
            
            if choice == '0':
                print("\n Returning to main menu...")
                break
            elif choice == '1':
                await _handle_login(service)
            elif choice == '2':
                await _handle_check_status(service)
            elif choice == '3':
                await _handle_set_location(service)
            elif choice == '4':
                await _handle_search(service)
            elif choice == '5':
                await _handle_view_cart(service)
            elif choice == '6':
                await _handle_add_to_cart(service)
            elif choice == '7':
                await _handle_checkout(service)
            elif choice == '8':
                await _handle_view_addresses(service)
            elif choice == '9':
                await _handle_select_upi(service)
            elif choice == '10':
                await _handle_pay_now(service)
            elif choice == '11':
                await _handle_logout(service, username)
                print("\n Session cleared. Returning to main menu...")
                break
            else:
                print("\n Invalid choice. Please try again.")
                
        except KeyboardInterrupt:
            print("\n\n Exiting Blinkit Integration Menu...")
            break
        except Exception as e:
            print(f"\n Error: {str(e)}")
            print("Please try again or contact support if the issue persists.")


async def _handle_logout(service: Any, username: str) -> None:
    """Handle Blinkit logout process."""
    print(f"\n Logging out user '{username}' from Blinkit...")
    try:
        # 1. Clear session from manager (this will also close browser)
        success = await clear_user_blinkit_service(username)
        
        # 2. Also clear session manager files for this user if they want a COMPLETELY fresh start
        # Actually, clear_user_blinkit_service already handles the in-memory part.
        # If we want to force re-login next time, we should ALSO clear the session files.
        from src.services.integrations.blinkit import get_session_manager
        session_mgr = get_session_manager()
        session_mgr.clear_session(username)
        
        if success:
            print(f"Successfully logged out '{username}' and cleared Blinkit session.")
        else:
            print(f"No active Blinkit session found for '{username}'. Session files cleared.")
            
    except Exception as e:
        print(f"Error during logout: {e}")
        from src.utils.helpers import log_error
        log_error("blinkit logout", e)


async def _handle_login(service: Any) -> None:
    """Handle Blinkit login process."""
    print("\n" + "="*60)
    print("BLINKIT LOGIN")
    print("="*60)
    
    try:
        # Check if already logged in
        is_logged_in = await service.check_login_status()
        if is_logged_in:
            print("\n You are already logged in!")
            return
        
        # Ensure service is initialized and browser is running
        await _ensure_initialized(service)
        
        # Verify page exists before proceeding
        if not service.ctx or not service.ctx.auth or not service.ctx.auth.page:
            print("\n Browser not properly initialized. Please try again.")
            # Force reinitialization
            service._initialized = False
            await service.initialize()
            if not service.ctx or not service.ctx.auth or not service.ctx.auth.page:
                print("\n Failed to initialize browser. Please check your setup.")
                return
        
        # Get phone number
        phone_number = input("\n Enter your phone number (10 digits): ").strip()
        if not phone_number or len(phone_number) != 10:
            print("Invalid phone number. Please enter 10 digits.")
            return
        
        # Start login process
        print(f"\n Sending OTP to {phone_number}...")
        print("Please check your phone for the OTP.")
        
        # Use the service's context
        try:
            await service.ctx.auth.login(phone_number)
        except Exception as e:
            print(f"\n  Error during login initiation: {str(e)}")
            print("Trying to reinitialize browser...")
            service._initialized = False
            await service.initialize()
            if service.ctx and service.ctx.auth:
                await service.ctx.auth.login(phone_number)
            else:
                raise
        
        # Wait for OTP
        otp = input("\n Enter the 4-digit OTP: ").strip()
        if not otp or len(otp) != 4:
            print("Invalid OTP. Please enter 4 digits.")
            return
        
        # Enter OTP
        try:
            await service.ctx.auth.enter_otp(otp)
        except Exception as e:
            print(f"\n  Error entering OTP: {str(e)}")
            return
        
        # Check if login successful
        await asyncio.sleep(2)  # Wait for login to process
        try:
            if await service.ctx.auth.is_logged_in():
                await service.ctx.auth.save_session()
                print("\n Login successful! Session saved.")
            else:
                print("\n Login failed. Please try again.")
                print("Make sure you entered the correct OTP.")
        except Exception as e:
            print(f"\n  Error verifying login: {str(e)}")
            print("Login may have succeeded. Try checking login status.")
            
    except Exception as e:
        print(f"\n Login error: {str(e)}")
        import traceback
        print(f"\n Error details: {traceback.format_exc()}")


async def _handle_check_status(service) -> None:
    """Check login status."""
    print("\n" + "="*60)
    print("LOGIN STATUS")
    print("="*60)
    
    try:
        is_logged_in = await service.check_login_status()
        if is_logged_in:
            print("\n You are logged into Blinkit!")
        else:
            print("\n You are not logged in.")
            print("Use option 1 to login.")
    except Exception as e:
        print(f"\n Error checking status: {str(e)}")


async def _handle_set_location(service) -> None:
    """Set delivery location."""
    print("\n" + "="*60)
    print("SET DELIVERY LOCATION")
    print("="*60)
    
    try:
        await _ensure_initialized(service)
        
        location = input("\n Enter your delivery location (e.g., 'Sector 62, Noida'): ").strip()
        if not location:
            print("Location cannot be empty.")
            return
        
        print(f"\n Setting location to {location}...")
        await service.order_manager.set_location(location)
        print("Location set successfully!")
        
    except Exception as e:
        print(f"\n Error setting location: {str(e)}")


async def _handle_search(service) -> None:
    """Search for products."""
    print("\n" + "="*60)
    print("SEARCH PRODUCTS")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        query = input("\n Enter product name to search: ").strip()
        if not query:
            print("Search query cannot be empty.")
            return
        
        print(f"\n Searching for '{query}'...")
        await service.order_manager.search_product(query)
        
        # Get results
        results = await service.order_manager.get_search_results(limit=10)
        
        if results:
            print(f"\n Found {len(results)} results:")
            print("-" * 60)
            for item in results:
                print(f"[{item['index']}] {item['name']} - {item['price']}")
                print(f"ID: {item['id']}")
        else:
            print("\n No results found.")
            
    except Exception as e:
        print(f"\n Search error: {str(e)}")


async def _handle_view_cart(service) -> None:
    """View cart contents."""
    print("\n" + "="*60)
    print("VIEW CART")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        cart_summary = await service.get_cart_summary()
        print(f"\n{cart_summary}")
        
    except Exception as e:
        print(f"\n Error viewing cart: {str(e)}")


async def _handle_add_to_cart(service) -> None:
    """Add item to cart."""
    print("\n" + "="*60)
    print("+ ADD TO CART")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        item_name = input("\n Enter product name to search and add: ").strip()
        if not item_name:
            print("Product name cannot be empty.")
            return
        
        quantity_input = input(" Enter quantity (default: 1): ").strip()
        quantity = int(quantity_input) if quantity_input.isdigit() else 1
        
        result = await service.search_and_add_item(item_name, quantity)
        
        if result['success']:
            print(f"\n {result['message']}")
        else:
            print(f"\n {result['message']}")
            
    except Exception as e:
        print(f"\n Error adding to cart: {str(e)}")


async def _handle_checkout(service) -> None:
    """Proceed to checkout."""
    print("\n" + "="*60)
    print("PROCEED TO CHECKOUT")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        result = await service.proceed_to_checkout()
        
        if result['success']:
            print(f"\n {result['message']}")
        else:
            print(f"\n {result['message']}")
            
    except Exception as e:
        print(f"\n Checkout error: {str(e)}")


async def _handle_view_addresses(service) -> None:
    """View saved addresses."""
    print("\n" + "="*60)
    print("SAVED ADDRESSES")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        addresses = await service.order_manager.get_saved_addresses()
        
        if isinstance(addresses, str) and "CRITICAL" in addresses:
            print(f"\n  {addresses}")
        elif addresses:
            print(f"\n Found {len(addresses)} saved addresses:")
            for addr in addresses:
                print(f"[{addr['index']}] {addr['label']} - {addr['details']}")
        else:
            print("\n No saved addresses found.")
            print("You may need to proceed to checkout first to see addresses.")
            
    except Exception as e:
        print(f"\n Error viewing addresses: {str(e)}")


async def _handle_select_upi(service) -> None:
    """Select UPI ID for payment."""
    print("\n" + "="*60)
    print("SELECT UPI ID")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        # Get available UPI IDs
        upi_ids = await service.order_manager.get_upi_ids()
        
        if upi_ids:
            print(f"\n Available UPI IDs:")
            for i, upi_id in enumerate(upi_ids, 1):
                print(f"{i}. {upi_id}")
            
            choice = input("\nEnter UPI ID number or type custom UPI ID: ").strip()
            
            if choice.isdigit() and 1 <= int(choice) <= len(upi_ids):
                selected_upi = upi_ids[int(choice) - 1]
            else:
                selected_upi = choice
            
            print(f"\n Selecting UPI ID: {selected_upi}...")
            await service.order_manager.select_upi_id(selected_upi)
            print("UPI ID selected!")
        else:
            print("\n No UPI IDs found.")
            print("You may need to proceed to checkout first.")
            
    except Exception as e:
        print(f"\n Error selecting UPI ID: {str(e)}")


async def _handle_pay_now(service) -> None:
    """Click Pay Now button."""
    print("\n" + "="*60)
    print("PAY NOW")
    print("="*60)
    
    try:
        if not await _verify_page_ready(service):
            print("\n Browser not ready. Please try again.")
            return
        
        print("\n Clicking Pay Now button...")
        await service.order_manager.click_pay_now()
        print("\n Payment initiated!")
        print("Please approve the payment on your UPI app.")
        
    except Exception as e:
        print(f"\n Payment error: {str(e)}")

# ===== From src/integration/blinkit_mcp.py =====

load_dotenv()

# Initialize FastMCP
SERVE_SSE = os.environ.get("SERVE_SSE", "").lower() == "true"
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "127.0.0.1")

if SERVE_SSE:
    mcp = FastMCP("blinkit-mcp", host=HOST, port=PORT)
else:
    mcp = FastMCP("blinkit-mcp")


# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class ProductVerificationError(Exception):
    """Raised when product verification fails after re-search."""
    pass

class QuantityLimitError(Exception):
    """Raised when quantity cannot be added to cart."""
    pass

class OTPInputError(Exception):
    """Raised when OTP input field cannot be detected."""
    pass

class SearchRetryError(Exception):
    """Raised when search fails after all retry attempts."""
    pass


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

import difflib
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('blinkit_mcp')

def calculate_similarity(str1: str, str2: str) -> float:
    """
    Calculate similarity between two strings using SequenceMatcher.
    Returns a float between 0 and 1, where 1 is identical.
    """
    return difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

async def check_network_connectivity() -> bool:
    """
    Check if blinkit.com is reachable.
    Returns True if network is available, False otherwise.
    """
    try:
        with urllib.request.urlopen("https://blinkit.com/favicon.ico", timeout=5) as response:
            return response.status == 200
    except Exception as e:
        logger.error(f"Network connectivity check failed: {e}")
        return False

def ensure_debug_dir() -> Path:
    """
    Ensure debug directory exists for screenshots.
    Returns the Path object for the debug directory.
    """
    debug_dir = Path.home() / ".blinkit_mcp" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    return debug_dir


# ============================================================================
# GEO UTILITIES
# ============================================================================

def get_current_location():
    """
    Fetches the current location (latitude, longitude) using ip-api.com.
    Returns a dictionary with 'latitude' and 'longitude' keys, or None if failed.
    """
    try:
        # Use a timeout of 3 seconds to avoid hanging
        with urllib.request.urlopen("http://ip-api.com/json/", timeout=3) as response:
            data = json.loads(response.read().decode())
            if data.get("status") == "success":
                return {"latitude": data.get("lat"), "longitude": data.get("lon")}
    except Exception as e:
        print(f"Error fetching location from IP API: {e}")
    return None


# ============================================================================
# BASE SERVICE
# ============================================================================

class BaseService:
    def __init__(self, page: Page, manager: Optional["BlinkitOrder"] = None):
        self.page = page
        self.manager = manager

    async def _is_store_closed(self):
        """Checks if the store is closed or unavailable."""
        if await self.page.is_visible("text=Store is closed"):
            print("CRITICAL: Store is closed.")
            return True
        return False


# ============================================================================
# SEARCH SERVICE
# ============================================================================

class SearchService(BaseService):
    async def search_product(self, product_name: str):
        """Searches for a product using the search bar with retry logic."""
        print(f"Searching for item: {product_name}...")
        if self.manager:
            self.manager.current_query = (
                product_name  # Store current query for state tracking
            )

        max_retries = 3
        backoff_delays = [5, 10, 15]  # seconds
        last_error = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # Check network connectivity before retry
                    logger.info(f"Checking network connectivity before retry {attempt + 1}...")
                    if not await check_network_connectivity():
                        error_msg = f"Network connectivity lost. Cannot retry search."
                        logger.error(error_msg)
                        raise SearchRetryError(error_msg)
                    
                    delay = backoff_delays[attempt - 1]
                    logger.info(f"Retrying search (attempt {attempt + 1}/{max_retries}) after {delay}s delay...")
                    await self.page.wait_for_timeout(delay * 1000)

                # 1. Activate Search
                if await self.page.is_visible("a[href='/s/']"):
                    await self.page.click("a[href='/s/']")
                elif await self.page.is_visible(
                    "div[class*='SearchBar__PlaceholderContainer']"
                ):
                    await self.page.click("div[class*='SearchBar__PlaceholderContainer']")
                else:
                    # Fallback: type directly if input is visible, or click generic search text
                    if await self.page.is_visible("input[placeholder*='Search']"):
                        await self.page.click("input[placeholder*='Search']")
                    else:
                        await self.page.click("text='Search'", timeout=3000)

                # 2. Type and Submit
                search_input = await self.page.wait_for_selector(
                    "input[placeholder*='Search'], input[type='text']",
                    state="visible",
                    timeout=30000,
                )
                await search_input.fill(product_name)
                await self.page.keyboard.press("Enter")

                # 3. Wait for results
                print("Waiting for results...")
                try:
                    # Wait for product cards
                    await self.page.wait_for_selector(
                        "div[role='button']:has-text('ADD')", timeout=30000
                    )
                    print("Search results loaded.")
                    logger.info(f"Search successful for '{product_name}' on attempt {attempt + 1}")
                    return  # Success!
                    
                except Exception as wait_error:
                    print(
                        "Timed out waiting for product cards. Checking for 'No results'..."
                    )
                    if await self.page.is_visible("text='No results found'"):
                        print("No results found for this query.")
                        logger.info(f"No results found for '{product_name}'")
                        return  # Not an error, just no results
                    else:
                        print("Could not detect standard product cards.")
                        raise wait_error  # Re-raise to trigger retry

            except Exception as e:
                last_error = e
                logger.warning(f"Search attempt {attempt + 1} failed: {e}")
                
                if attempt == max_retries - 1:
                    # Final attempt failed
                    error_msg = (
                        f"Search failed after {max_retries} attempts. "
                        f"Last error: {last_error}"
                    )
                    logger.error(error_msg)
                    raise SearchRetryError(error_msg) from last_error
                # Continue to next retry


    async def get_search_results(self, limit=20):
        """
        Enhanced: Parses search results with availability and metadata.
        Phase 10: Intelligent product selection.
        """
        results = []
        try:
            cards = (
                self.page.locator("div[role='button']")
                .filter(has_text="ADD")
                .filter(has_text="₹")
            )

            count = await cards.count()
            print(f"Found {count} product cards.")

            for i in range(min(count, limit)):
                card = cards.nth(i)
                text_content = await card.inner_text()

                # Extract ID
                product_id = await card.get_attribute("id")
                if not product_id:
                    product_id = "unknown"

                # Extract Name
                name_locator = card.locator("div[class*='line-clamp-2']")
                if await name_locator.count() > 0:
                    name = await name_locator.first.inner_text()
                else:
                    lines = [line for line in text_content.split("\n") if line.strip()]
                    name = lines[0] if lines else "Unknown Product"

                # Store in known products including the source query
                if product_id != "unknown" and self.manager:
                    self.manager.known_products[product_id] = {
                        "source_query": self.manager.current_query,
                        "name": name,
                    }

                # Extract Price and parse numeric value
                price_str = "Unknown Price"
                price_numeric = None
                if "₹" in text_content:
                    for part in text_content.split("\n"):
                        if "₹" in part:
                            price_str = part.strip()
                            # Extract numeric price
                            import re
                            price_match = re.search(r'₹\s*(\d+(?:\.\d+)?)', price_str)
                            if price_match:
                                price_numeric = float(price_match.group(1))
                            break
                
                # Check availability (ADD button present means available)
                add_button = card.locator("div").filter(has_text="ADD")
                is_available = await add_button.count() > 0
                
                # Extract pack size/unit info (from name or separate field)
                pack_size = None
                unit = None
                # Common patterns: "500 g", "1 L", "1 kg", "6 pack"
                import re
                size_match = re.search(r'(\d+(?:\.\d+)?)\s*(g|kg|ml|l|pack|piece|pc)', 
                                      name.lower())
                if size_match:
                    pack_size = float(size_match.group(1))
                    unit = size_match.group(2)
                
                # Check for discount/offer indicators
                has_offer = (
                    "off" in text_content.lower() or 
                    "save" in text_content.lower() or
                    "offer" in text_content.lower()
                )

                results.append({
                    "index": i,
                    "id": product_id,
                    "name": name,
                    "price": price_str,
                    "price_numeric": price_numeric,
                    "is_available": is_available,
                    "pack_size": pack_size,
                    "unit": unit,
                    "has_offer": has_offer,
                    "raw_text": text_content
                })

        except Exception as e:
            print(f"Error extracting search results: {e}")

        return results



# ============================================================================
# LOCATION SERVICE
# ============================================================================

class LocationService(BaseService):
    async def set_location(self, location_name: str):
        """Sets the location manually."""
        print(f"Setting location to: {location_name}...")
        try:
            # Check if the modal is open, if not try to open it
            if not await self.page.is_visible("input[name='select-locality']"):
                # Click location bar
                if await self.page.is_visible("div[class*='LocationBar__Container']"):
                    await self.page.click("div[class*='LocationBar__Container']")

            # Wait for input
            location_input = await self.page.wait_for_selector(
                "input[name='select-locality'], input[placeholder*='search delivery location']",
                state="visible",
                timeout=30000,
            )

            await location_input.fill(location_name)
            await self.page.wait_for_timeout(1000)

            # Select first result
            first_result = self.page.locator(
                "div[class*='LocationSearchBox__LocationItemContainer']"
            ).first
            if await first_result.is_visible():
                await first_result.click()
                print("Selected first location result.")

                # Wait for location update
                await self.page.wait_for_timeout(2000)

                # Check if this new location is unavailable
                if await self.page.is_visible("text=Currently unavailable"):
                    print(
                        "WARNING: Store is marked as 'Currently unavailable' at this new location."
                    )
            else:
                print("No location results found.")

        except Exception as e:
            print(f"Error setting location: {e}")

    async def get_saved_addresses(self):
        """Scrapes saved addresses from the selection modal."""
        print("Checking for address selection modal...")
        try:
            if not await self.page.is_visible("text='Select delivery address'"):
                print("Address selection modal not visible.")
                return []

            if await self._is_store_closed():
                return "CRITICAL: Store is closed."

            print("Address modal detected. Parsing addresses...")
            address_items = self.page.locator(
                "div[class*='AddressList__AddressItemWrapper']"
            )
            count = await address_items.count()

            addresses = []
            for i in range(count):
                item = address_items.nth(i)
                # Parse label
                label_el = item.locator("div[class*='AddressList__AddressLabel']")
                if await label_el.count() > 0:
                    label = await label_el.inner_text()
                else:
                    label = "Unknown"

                # Parse details
                details_el = item.locator(
                    "div[class*='AddressList__AddressDetails']"
                ).last
                if await details_el.count() > 0:
                    details = await details_el.inner_text()
                else:
                    details = ""

                addresses.append({"index": i, "label": label, "details": details})
            return addresses

        except Exception as e:
            print(f"Error getting addresses: {e}")
            return []

    async def select_address(self, index: int):
        """Selects an address by index."""
        try:
            items = self.page.locator("div[class*='AddressList__AddressItemWrapper']")
            if index < await items.count():
                print(f"Selecting address at index {index}...")

                if await self._is_store_closed():
                    return "CRITICAL: Store is closed."

                await items.nth(index).click()
                # Wait for modal to close or location to update
                await self.page.wait_for_timeout(2000)
            else:
                print(f"Invalid address index: {index}")
        except Exception as e:
            print(f"Error selecting address: {e}")


# ============================================================================
# CART SERVICE
# ============================================================================

class CartService(BaseService):
    async def add_to_cart(self, product_id: str, quantity: int = 1):
        """Adds a product to the cart by its unique ID. Supports multiple quantities."""
        print(f"Adding product with ID {product_id} to cart (Quantity: {quantity})...")
        actual_quantity_added = 0
        
        try:
            # Target the specific card by ID
            card = self.page.locator(f"div[id='{product_id}']")

            if await card.count() == 0:
                print(f"Product ID {product_id} not found on current page.")

                # Check if we know this product from a previous search
                if self.manager and product_id in self.manager.known_products:
                    logger.info(f"Product {product_id} found in history. Attempting recovery...")
                    product_info = self.manager.known_products[product_id]
                    source_query = product_info.get("source_query")
                    expected_name = product_info.get("name")

                    if source_query:
                        logger.info(f"Navigating back to search results for '{source_query}'...")
                        print(f"Navigating back to search results for '{source_query}'...")
                        
                        # Delegate search back to manager/search service
                        if hasattr(self.manager, "search_product"):
                            await self.manager.search_product(source_query)

                        # Re-locate the card after search
                        card = self.page.locator(f"div[id='{product_id}']")
                        if await card.count() == 0:
                            error_msg = f"Product {product_id} not found after re-search for '{source_query}'"
                            logger.error(error_msg)
                            raise ProductVerificationError(error_msg)
                        
                        # CRITICAL: Verify this is the same product
                        logger.info(f"Verifying product identity after re-search...")
                        name_locator = card.locator("div[class*='line-clamp-2']")
                        if await name_locator.count() > 0:
                            actual_name = await name_locator.first.inner_text()
                        else:
                            # Fallback: get text from card
                            card_text = await card.inner_text()
                            lines = [line for line in card_text.split("\n") if line.strip()]
                            actual_name = lines[0] if lines else "Unknown"
                        
                        # Calculate similarity
                        similarity = calculate_similarity(expected_name, actual_name)
                        logger.info(f"Product name similarity: {similarity:.2%} (expected: '{expected_name}', actual: '{actual_name}')")
                        
                        if similarity < 0.8:  # 80% threshold
                            error_msg = (
                                f"Product verification failed for {product_id}. "
                                f"Expected: '{expected_name}', Found: '{actual_name}' "
                                f"(Similarity: {similarity:.2%})"
                            )
                            logger.error(error_msg)
                            raise ProductVerificationError(error_msg)
                        
                        logger.info(f"Product verification successful for {product_id}")
                    else:
                        error_msg = f"No source query found for product {product_id}"
                        logger.error(error_msg)
                        raise ProductVerificationError(error_msg)
                else:
                    error_msg = f"Product ID {product_id} unknown and not on current page"
                    logger.error(error_msg)
                    raise ProductVerificationError(error_msg)

            # Find the ADD button specifically inside the card
            add_btn = card.locator("div").filter(has_text="ADD").last

            items_to_add = quantity

            # If ADD button is visible, click it once to start
            if await add_btn.is_visible():
                await add_btn.click()
                print(f"Clicked ADD button for {product_id} (1/{quantity}).")
                actual_quantity_added = 1
                items_to_add -= 1
                # Wait for the counter to appear
                await self.page.wait_for_timeout(1000)

            # Use increment button for remaining quantity
            if items_to_add > 0:
                # Wait for the counter to initialize
                await self.page.wait_for_timeout(2000)

                # Robust strategy to find the + button
                plus_btn = card.locator(".icon-plus").first
                if await plus_btn.count() > 0:
                    plus_btn = plus_btn.locator("..")
                else:
                    plus_btn = card.locator("text='+'").first

                if await plus_btn.is_visible():
                    for i in range(items_to_add):
                        await plus_btn.click()
                        actual_quantity_added += 1
                        print(
                            f"Incrementing quantity for {product_id} ({actual_quantity_added}/{quantity})."
                        )
                        # Check for limit reached
                        try:
                            limit_msg = self.page.get_by_text(
                                "Sorry, you can't add more of this item"
                            )
                            if await limit_msg.is_visible(timeout=1000):
                                logger.warning(f"Quantity limit reached for {product_id} at {actual_quantity_added} items")
                                print(f"Quantity limit reached for {product_id}.")
                                break
                        except Exception:
                            pass

                        await self.page.wait_for_timeout(1000)
                else:
                    # Try fallback: direct input field
                    logger.warning(f"'+' button not visible for {product_id}. Trying input field fallback...")
                    quantity_input = card.locator("input[type='number'], input[type='text']").first
                    
                    if await quantity_input.count() > 0 and await quantity_input.is_visible():
                        try:
                            await quantity_input.fill(str(quantity))
                            await self.page.keyboard.press("Enter")
                            await self.page.wait_for_timeout(1000)
                            actual_quantity_added = quantity
                            logger.info(f"Successfully set quantity via input field for {product_id}")
                        except Exception as input_error:
                            logger.error(f"Input field fallback failed: {input_error}")
                            error_msg = (
                                f"Could not add remaining quantity for {product_id}. "
                                f"Requested: {quantity}, Added: {actual_quantity_added}. "
                                f"'+' button not found and input field fallback failed."
                            )
                            raise QuantityLimitError(error_msg)
                    else:
                        error_msg = (
                            f"Could not add remaining quantity for {product_id}. "
                            f"Requested: {quantity}, Added: {actual_quantity_added}. "
                            f"'+' button not found and no input field available."
                        )
                        logger.error(error_msg)
                        raise QuantityLimitError(error_msg)

            await self.page.wait_for_timeout(2000)

            # Check for "Store Unavailable" modal
            if await self.page.is_visible(
                "div:has-text('Sorry, can\\'t take your order')"
            ):
                print("WARNING: Store is unavailable (Modal detected).")
                return {"success": False, "quantity_added": actual_quantity_added, "quantity_requested": quantity, "error": "Store unavailable"}

            logger.info(f"Successfully added {actual_quantity_added} of {quantity} requested items for {product_id}")
            return {"success": True, "quantity_added": actual_quantity_added, "quantity_requested": quantity}

        except (ProductVerificationError, QuantityLimitError) as e:
            # Re-raise custom exceptions
            raise
        except Exception as e:
            logger.error(f"Error adding to cart: {e}")
            print(f"Error adding to cart: {e}")
            return {"success": False, "quantity_added": actual_quantity_added, "quantity_requested": quantity, "error": str(e)}

    async def remove_from_cart(self, product_id: str, quantity: int = 1):
        """Removes a specific quantity of a product from the cart."""
        print(f"Removing {quantity} of product ID {product_id} from cart...")
        try:
            # Target the specific card by ID
            card = self.page.locator(f"div[id='{product_id}']")

            if await card.count() == 0:
                # Attempt recovery via search if known
                if self.manager and product_id in self.manager.known_products:
                    product_info = self.manager.known_products[product_id]
                    source_query = product_info.get("source_query")
                    if source_query:
                        if hasattr(self.manager, "search_product"):
                            await self.manager.search_product(source_query)
                        card = self.page.locator(f"div[id='{product_id}']")
                        if await card.count() == 0:
                            print(
                                f"Product {product_id} not found after recovery search."
                            )
                            return
                else:
                    print(f"Product ID {product_id} not found and unknown.")
                    return

            # Check for decrement button
            minus_btn = card.locator(".icon-minus").first
            if await minus_btn.count() > 0:
                minus_btn = minus_btn.locator("..")
            else:
                minus_btn = card.locator("text='-'").first

            if await minus_btn.is_visible():
                for i in range(quantity):
                    await minus_btn.click()
                    print(
                        f"Decrementing quantity for {product_id} ({i + 1}/{quantity})."
                    )
                    await self.page.wait_for_timeout(500)

                    # If ADD button reappears, item is fully removed
                    if (
                        await card.locator("div")
                        .filter(has_text="ADD")
                        .last.is_visible()
                    ):
                        print(f"Item {product_id} completely removed from cart.")
                        break
            else:
                print(f"Item {product_id} is not in cart (no '-' button found).")

        except Exception as e:
            print(f"Error removing from cart: {e}")

    async def get_cart_items(self):
        """Checks items in the cart and returns the text content."""
        try:
            cart_btn = self.page.locator(
                "div[class*='CartButton__Button'], div[class*='CartButton__Container']"
            )

            if await cart_btn.count() > 0:
                await cart_btn.first.click()

                # Verify opening
                try:
                    await self.page.wait_for_timeout(2000)

                    # 1. Critical Availability Check
                    if (
                        await self.page.is_visible("text=Sorry, can't take your order")
                        or await self.page.is_visible("text=Currently unavailable")
                        or await self.page.is_visible("text=High Demand")
                    ):
                        return "CRITICAL: Store is unavailable. 'Sorry, can't take your order'. Please try again later."

                    # 2. Check for Bill Details or Proceed Button
                    is_cart_active = (
                        await self.page.is_visible("text=/Bill details/i")
                        or await self.page.is_visible("button:has-text('Proceed')")
                        or await self.page.is_visible("text=ordering for")
                    )

                    if await self._is_store_closed():
                        return "CRITICAL: Store is closed."

                    # Scrape content
                    drawer = self.page.locator(
                        "div[class*='CartDrawer'], div[class*='CartSidebar'], div.cart-modal-rn, div[class*='CartWrapper__CartContainer']"
                    ).first

                    if await drawer.count() > 0:
                        content = await drawer.inner_text()
                        if (
                            "Currently unavailable" in content
                            or "can't take your order" in content
                        ):
                            return "CRITICAL: Store is unavailable (Text detected in cart). Please try again later."
                        print(
                            "You can select address, or checkout, if you want to place the order."
                        )
                        return content

                    if is_cart_active:
                        return "Cart is open. (Could not scrape specific drawer content, but functionality is active)."
                    else:
                        return "WARNING: Cart opened but seems empty or store is unavailable (No bill details/proceed button found)."

                except Exception as e:
                    return f"Cart drawer checking timed out or error: {e}"
            else:
                return "Cart button not found."

        except Exception as e:
            return f"Error getting cart items: {e}"


# ============================================================================
# CHECKOUT SERVICE
# ============================================================================

class CheckoutService(BaseService):
    async def place_order(self):
        """Proceeds to checkout."""

        if await self._is_store_closed():
            return "CRITICAL: Store is closed."

        try:
            proceed_btn = (
                self.page.locator("button, div").filter(has_text="Proceed").last
            )

            # If Proceed not visible, try opening the cart first
            if not await proceed_btn.is_visible():
                print("Proceed button not visible. Attempting to open Cart drawer...")
                cart_btn = self.page.locator(
                    "div[class*='CartButton__Button'], div[class*='CartButton__Container']"
                )
                if await cart_btn.count() > 0:
                    await cart_btn.first.click()
                    print("Clicked 'My Cart' button.")
                    await self.page.wait_for_timeout(2000)
                else:
                    print("Could not find 'My Cart' button.")

            # Try clicking Proceed again
            if await proceed_btn.is_visible():
                await proceed_btn.click()
                print(
                    "Cart checkout successfully.\nYou can select the payment method and proceed to pay."
                )
                await self.page.wait_for_timeout(3000)
            else:
                print(
                    "Proceed button not visible. Cart might be empty or Store Unavailable."
                )

        except Exception as e:
            print(f"Error placing order: {e}")

    async def get_upi_ids(self):
        """Scrapes available UPI IDs/options from the payment widget."""
        print("Getting available UPI IDs...")
        try:
            iframe_element = await self.page.wait_for_selector(
                "#payment_widget", timeout=30000
            )
            if not iframe_element:
                print("Payment widget iframe not found.")
                return []

            frame = await iframe_element.content_frame()
            if not frame:
                return []

            await frame.wait_for_load_state("networkidle")

            ids = []
            # Try to find elements that look like VPAs (contain @) inside the frame
            vpa_locators = frame.locator("text=/@/")
            count = await vpa_locators.count()
            for i in range(count):
                text = await vpa_locators.nth(i).inner_text()
                if "@" in text:
                    ids.append(text.strip())

            # Also add "Add new UPI ID" option if exists
            if await frame.locator("text='Add new UPI ID'").count() > 0:
                ids.append("Add new UPI ID")

            print(f"Found UPI IDs: {ids}")
            return ids

        except Exception as e:
            print(f"Error getting UPI IDs: {e}")
            return []

    async def select_upi_id(self, upi_id: str):
        """Selects a specific UPI ID or enters a new one."""
        print(f"Selecting UPI ID: {upi_id}...")
        try:
            iframe_element = await self.page.wait_for_selector(
                "#payment_widget", timeout=30000
            )
            if not iframe_element:
                return

            frame = await iframe_element.content_frame()
            if not frame:
                return

            # 1. Try to click on an existing saved VPA if it matches
            saved_vpa = frame.locator(f"text='{upi_id}'")
            if await saved_vpa.count() > 0:
                await saved_vpa.first.click()
                print(f"Clicked saved VPA: {upi_id}")
                return

            # 2. If not found, Select "UPI" / "Add new UPI ID" section
            # Click generic UPI header first if needed to expand
            upi_header = frame.locator("div").filter(has_text="UPI").last
            if await upi_header.count() > 0:
                await upi_header.click()

            await self.page.wait_for_timeout(500)

            # 3. Enter VPA in input
            input_locator = frame.locator(
                "input[placeholder*='UPI'], input[type='text']"
            )
            if await input_locator.count() > 0:
                await input_locator.first.fill(upi_id)
                print(f"Filled UPI ID: {upi_id}")

                # Verify
                verify_btn = frame.locator("text=Verify")
                if await verify_btn.count() > 0:
                    await verify_btn.click()
                    print("Clicked Verify button.")
            else:
                print("Could not find UPI input field.")

        except Exception as e:
            print(f"Error selecting UPI ID: {e}")

    async def click_pay_now(self):
        """Clicks the final Pay Now button."""
        try:
            # Strategy 1: Specific class partial match
            pay_btn_specific = self.page.locator(
                "div[class*='Zpayments__Button']:has-text('Pay Now')"
            )
            if (
                await pay_btn_specific.count() > 0
                and await pay_btn_specific.first.is_visible()
            ):
                await pay_btn_specific.first.click()
                print("Clicked 'Pay Now'. Please approve the payment on your UPI app.")
                return

            # Strategy 2: Text match on page
            pay_btn_text = (
                self.page.locator("div, button").filter(has_text="Pay Now").last
            )
            if await pay_btn_text.count() > 0 and await pay_btn_text.is_visible():
                await pay_btn_text.click()
                print("Clicked 'Pay Now'. Please approve the payment on your UPI app.")
                return

            # Strategy 3: Check inside iframe
            iframe_element = await self.page.query_selector("#payment_widget")
            if iframe_element:
                frame = await iframe_element.content_frame()
                if frame:
                    frame_btn = frame.locator("text='Pay Now'")
                    if await frame_btn.count() > 0:
                        await frame_btn.first.click()
                        print("Clicked 'Pay Now' inside iframe.")
                        return

            print("Could not find 'Pay Now' button (timeout or not in DOM).")

        except Exception as e:
            print(f"Error clicking Pay Now: {e}")


# ============================================================================
# BLINKIT ORDER MANAGER
# ============================================================================

class BlinkitOrder:
    def __init__(self, page: Page):
        self.page = page

        # State tracking for cross-search cart addition
        self.known_products = {}  # Maps product_id -> {'source_query': str, 'name': str}
        self.current_query = None

        # Initialize Services
        self.search_service = SearchService(page, self)
        self.location_service = LocationService(page, self)
        self.cart_service = CartService(page, self)
        self.checkout_service = CheckoutService(page, self)

        # Attach blocking listener for debugging specific relevant errors
        self.page.on("response", self._handle_response)

    async def _handle_response(self, response):
        """Monitor network responses for payment failures."""
        try:
            url = response.url
            if "zpaykit" in url or "payment" in url:
                if response.status >= 400:
                    print(f"DEBUG: Payment API Error {response.status} at {url}")

                # Try to parse JSON for failure messages even on 200 OK
                if "application/json" in response.headers.get("content-type", ""):
                    try:
                        data = await response.json()
                        if isinstance(data, dict) and (
                            data.get("status") == "failed" or data.get("error")
                        ):
                            print(f"DEBUG: Payment API Failure captured: {data}")
                    except Exception:
                        pass
        except Exception:
            pass

    # --- Search Delegate ---
    async def search_product(self, product_name: str):
        return await self.search_service.search_product(product_name)

    async def get_search_results(self, limit=10):
        return await self.search_service.get_search_results(limit)

    # --- Location Delegate ---
    async def set_location(self, location_name: str):
        return await self.location_service.set_location(location_name)

    async def get_saved_addresses(self):
        return await self.location_service.get_saved_addresses()

    async def select_address(self, index: int):
        return await self.location_service.select_address(index)

    # --- Cart Delegate ---
    async def add_to_cart(self, product_id: str, quantity: int = 1):
        return await self.cart_service.add_to_cart(product_id, quantity)

    async def remove_from_cart(self, product_id: str, quantity: int = 1):
        return await self.cart_service.remove_from_cart(product_id, quantity)

    async def get_cart_items(self):
        return await self.cart_service.get_cart_items()

    # --- Checkout Delegate ---
    async def place_order(self):
        return await self.checkout_service.place_order()

    async def get_upi_ids(self):
        return await self.checkout_service.get_upi_ids()

    async def select_upi_id(self, upi_id: str):
        return await self.checkout_service.select_upi_id(upi_id)

    async def click_pay_now(self):
        return await self.checkout_service.click_pay_now()


# ============================================================================
# BLINKIT AUTH
# ============================================================================

class BlinkitAuth:
    def __init__(self, headless: bool = False, session_path: str = None):
        self.headless = headless
        if session_path:
            self.session_path = session_path
        else:
            # Use a safe directory in home folder to avoid permission/read-only issues
            self.session_path = os.path.expanduser("~/.blinkit_mcp/cookies/auth.json")

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start_browser(self):
        """Starts the Playwright browser (Firefox)."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.firefox.launch(headless=self.headless)

        # Default fallback (Noida Sector 62)
        geolocation = {"latitude": 28.6279, "longitude": 77.3649}

        try:
            detected_loc = get_current_location()
            if detected_loc:
                print(f"Using detected location: {detected_loc}")
                geolocation = detected_loc
            else:
                print("Could not detect location. Using fallback (Noida).")
        except Exception as e:
            print(f"Error initializing location detection: {e}. Using fallback.")

        if os.path.exists(self.session_path):
            print(f"Loading session from {self.session_path}")
            self.context = await self.browser.new_context(
                storage_state=self.session_path,
                permissions=["geolocation"],
                geolocation=geolocation,
            )
        else:
            print("No existing session found. Starting fresh.")
            self.context = await self.browser.new_context(
                permissions=["geolocation"],
                geolocation=geolocation,
            )

        self.page = await self.context.new_page()
        try:
            # Set a longer timeout (60s) and wait for 'domcontentloaded' which is faster than 'load'
            await self.page.goto(
                "https://blinkit.com/", timeout=60000, wait_until="domcontentloaded"
            )
            print("Opened Blinkit.com")
        except Exception as e:
            print(
                f"Warning: Navigation to Blinkit took too long or failed: {e}. Attempting to proceed regardless."
            )

        # Handle "Detect my location" popup if it appears
        try:
            print("Checking for location popup...")
            location_btn = self.page.locator("button", has_text="Detect my location")
            try:
                # Wait briefly to see if it appears
                await location_btn.wait_for(state="visible", timeout=3000)
                print("Location popup detected. Clicking 'Detect my location'...")
                await location_btn.click()
                # Wait for it to disappear potentially
                await location_btn.wait_for(state="hidden", timeout=5000)
            except Exception:
                # Timed out waiting for it, probably didn't appear or already handled
                pass
        except Exception as e:
            print(f"Note: Error checking location popup: {e}")

        # Check for global unavailability message on Homepage
        try:
            if await self.page.is_visible("text=Currently unavailable"):
                print(
                    "WARNING: Store is marked as 'Currently unavailable' on the homepage."
                )
        except Exception:
            pass

    async def login(self, phone_number: str):
        """Initiates the login process with a phone number."""
        print(f"Attempting to log in with {phone_number}...")
        
        # Verify page is ready
        if not self.page:
            print("Page not ready. Restarting browser...")
            await self.start_browser()
        else:
            try:
                # Check if page is closed or event loop is closed
                if self.page.is_closed():
                    print("Page closed. Restarting browser...")
                    await self.start_browser()
            except RuntimeError as e:
                # Event loop is closed, restart browser
                if "Event loop is closed" in str(e) or "closed" in str(e).lower():
                    print("Browser session tied to closed event loop. Restarting browser...")
                    await self.start_browser()
                else:
                    raise
        
        # 1. Click Login Button
        try:
            # Try to wait for page to be ready, but handle event loop issues gracefully
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            except RuntimeError as e:
                # If event loop is closed, page is invalid
                if "Event loop is closed" in str(e) or "closed" in str(e).lower():
                    print("Page tied to closed event loop. Restarting browser...")
                    await self.start_browser()
                    await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                else:
                    raise
            
            # Try multiple strategies to find the Login button
            if await self.page.is_visible("text='Login'"):
                await self.page.click("text='Login'")
                print("Clicked 'Login' text.")
            elif await self.page.is_visible("div[class*='ProfileButton__Container']"):
                await self.page.locator(
                    "div[class*='ProfileButton__Container']"
                ).click()
                print("Clicked ProfileButton container.")
            else:
                print(
                    "Could not find explicit Login button. Checking if already on login screen..."
                )
        except Exception as e:
            print(f"Error clicking login button: {e}")
            raise  # Re-raise to signal failure

        # 2. Wait for Login Modal / Phone Input
        try:
            print("Waiting for phone number input...")
            # Increased timeout and generic selector
            phone_input = await self.page.wait_for_selector(
                "input[type='tel'], input[name='mobile'], input[type='text']",
                state="visible",
                timeout=30000,
            )

            if phone_input:
                await phone_input.click()
                await phone_input.fill(phone_number)
                print(f"Filled phone number: {phone_number}")

                # 3. Submit Phone Number
                await self.page.wait_for_timeout(500)  # slight delay for UI update

                # Check for "Get OTP" or "Next"
                if await self.page.is_visible("text='Next'"):
                    await self.page.click("text='Next'")
                elif await self.page.is_visible("text='Continue'"):
                    await self.page.click("text='Continue'")
                else:
                    # Fallback: press Enter on the input
                    await self.page.keyboard.press("Enter")
                    print("Pressed Enter to submit.")

            else:
                print("Phone input found but None returned?")

        except Exception as e:
            print(f"Error entering phone number: {e}")

    async def enter_otp(self, otp: str):
        """Enters the OTP with priority-based field detection."""
        try:
            print("Waiting for OTP input...")
            logger.info("Starting OTP entry process...")
            
            # Wait for any input to be visible
            await self.page.wait_for_selector("input", timeout=30000)
            
            debug_dir = ensure_debug_dir()
            timestamp = asyncio.get_event_loop().time()
            
            # Capture screenshot before OTP entry
            screenshot_before = debug_dir / f"otp_before_{timestamp}.png"
            await self.page.screenshot(path=str(screenshot_before))
            logger.info(f"Screenshot saved: {screenshot_before}")

            strategy_used = None
            success = False

            # Priority 1: data-test-id='otp-input'
            otp_input = self.page.locator("input[data-test-id='otp-input']")
            if await otp_input.count() > 0 and await otp_input.first.is_visible():
                logger.info("Strategy 1: Using data-test-id='otp-input'")
                # Validate it accepts numeric input
                input_type = await otp_input.first.get_attribute("type")
                input_mode = await otp_input.first.get_attribute("inputmode")
                
                if input_type in ["tel", "number", "text"] or input_mode in ["numeric", "tel"]:
                    await otp_input.first.fill(otp)
                    strategy_used = "data-test-id"
                    success = True
                else:
                    logger.warning(f"Input field type '{input_type}' may not accept numeric input")

            # Priority 2: name/id containing 'otp'
            if not success:
                otp_input = self.page.locator("input[name*='otp'], input[id*='otp']")
                if await otp_input.count() > 0 and await otp_input.first.is_visible():
                    logger.info("Strategy 2: Using input[name*='otp'] or input[id*='otp']")
                    input_type = await otp_input.first.get_attribute("type")
                    
                    if input_type in ["tel", "number", "text", None]:
                        await otp_input.first.fill(otp)
                        strategy_used = "name_id_otp"
                        success = True

            # Priority 3: Four separate digit inputs
            if not success:
                inputs = self.page.locator("input")
                count = await inputs.count()
                
                if count == 4:
                    logger.info("Strategy 3: Detected 4 separate OTP inputs")
                    # Verify they're numeric inputs
                    all_numeric = True
                    for i in range(4):
                        input_type = await inputs.nth(i).get_attribute("type")
                        if input_type not in ["tel", "number", "text", None]:
                            all_numeric = False
                            break
                    
                    if all_numeric and len(otp) >= 4:
                        for i, digit in enumerate(otp[:4]):
                            await inputs.nth(i).fill(digit)
                            await self.page.wait_for_timeout(100)
                        strategy_used = "four_inputs"
                        success = True
                    else:
                        logger.warning("4 inputs found but validation failed")

            # Priority 4: Generic input[type='text'] or input[type='tel']
            if not success:
                otp_input = self.page.locator("input[type='text'], input[type='tel'], input[type='number']").first
                if await otp_input.count() > 0 and await otp_input.is_visible():
                    logger.info("Strategy 4: Using generic input field")
                    await otp_input.fill(otp)
                    strategy_used = "generic_input"
                    success = True

            if not success:
                # Capture screenshot on failure
                screenshot_fail = debug_dir / f"otp_failed_{timestamp}.png"
                await self.page.screenshot(path=str(screenshot_fail))
                error_msg = (
                    f"Could not find suitable OTP input field after trying all strategies. "
                    f"Screenshot saved to {screenshot_fail}"
                )
                logger.error(error_msg)
                raise OTPInputError(error_msg)

            # Capture screenshot after OTP entry
            screenshot_after = debug_dir / f"otp_after_{timestamp}.png"
            await self.page.screenshot(path=str(screenshot_after))
            logger.info(f"Screenshot saved: {screenshot_after}")

            print(f"Entered OTP using strategy: {strategy_used}. Waiting for auto-submit or button...")
            logger.info(f"OTP entered successfully using strategy: {strategy_used}")
            
            await self.page.keyboard.press("Enter")
            
            return {"success": True, "strategy": strategy_used}

        except OTPInputError:
            # Re-raise custom exception
            raise
        except Exception as e:
            error_msg = f"Error entering OTP: {e}"
            logger.error(error_msg)
            print(error_msg)
            raise OTPInputError(error_msg) from e

    async def is_logged_in(self) -> bool:
        """Checks if the user is logged in."""
        if not self.page or self.page.is_closed():
            return False

        try:
            if await self.page.is_visible(
                "text=My Account"
            ) or await self.page.is_visible(".user-profile"):
                return True

            if not await self.page.is_visible("text=Login"):
                return True

            return False
            return False
        except Exception:
            return False

    async def save_session(self):
        """Saves functionality cookies to file."""
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.session_path), exist_ok=True)
        await self.context.storage_state(path=self.session_path)
        print(f"Session saved to {self.session_path}")

    async def close(self):
        """Closes the browser."""
        try:
            if self.browser:
                await self.browser.close()
            self.browser = None
        except Exception:
            # Silence errors during browser close as we are cleaning up
            pass
            
        try:
            if self.playwright:
                await self.playwright.stop()
            self.playwright = None
        except Exception:
            pass


# ============================================================================
# GLOBAL CONTEXT
# ============================================================================

class BlinkitContext:
    def __init__(self, session_path: Optional[str] = None):
        """
        Initialize Blinkit context.
        
        Args:
            session_path: Optional custom path for session storage.
                         If None, uses default global path (NOT RECOMMENDED for multi-user).
        """
        # Use custom session path if provided, otherwise use default
        if session_path:
            # Custom per-user session path
            session_file = Path(session_path)
        else:
            # Default global session path (legacy, not recommended)
            session_dir = Path.home() / ".blinkit_mcp" / "cookies"
            session_dir.mkdir(parents=True, exist_ok=True)
            session_file = session_dir / "auth.json"

        headless = os.environ.get("HEADLESS", "false").lower() == "true"
        self.auth = BlinkitAuth(headless=headless, session_path=str(session_file))
        self.order = None

    async def ensure_started(self):
        restart = False
        if not self.auth.page:
            restart = True
        else:
            try:
                if self.auth.page.is_closed():
                    restart = True
            except Exception:
                restart = True

        if restart:
            print("Browser not active or closed. Launching...")
            await self.auth.start_browser()
            self.order = BlinkitOrder(self.auth.page)
        elif self.order is None and self.auth.page:
            self.order = BlinkitOrder(self.auth.page)


ctx = BlinkitContext()


# ============================================================================
# MCP TOOLS
# ============================================================================

@mcp.tool()
async def check_login() -> str:
    await ctx.ensure_started()
    if await ctx.auth.is_logged_in():
        await ctx.auth.save_session()
        return "Logged In"
    return "Not Logged In"


@mcp.tool()
async def set_location(location_name: str) -> str:
    await ctx.ensure_started()
    await ctx.order.set_location(location_name)
    return f"Location search initiated for {location_name}. Please check result."


@mcp.tool()
async def login(phone_number: str) -> str:
    await ctx.ensure_started()

    if await ctx.auth.is_logged_in():
        return "Already logged in with valid session."

    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.auth.login(phone_number)
    return f.getvalue()


@mcp.tool()
async def enter_otp(otp: str) -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.auth.enter_otp(otp)
        if await ctx.auth.is_logged_in():
            await ctx.auth.save_session()
            print("Session saved successfully.")
        else:
            print("Login verification might have failed or is still processing.")
    return f.getvalue()


@mcp.tool()
async def search(query: str) -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.search_product(query)
        results = await ctx.order.get_search_results()
        if results:
            print(f"\nFound {len(results)} results:")
            for item in results:
                print(f"[{item['index']}] ID: {item['id']} | {item['name']} - {item['price']}")
        else:
            print("No results found.")
    return f.getvalue()


@mcp.tool()
async def add_to_cart(item_id: str, quantity: int = 1) -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.add_to_cart(item_id, quantity)
    return f.getvalue()


@mcp.tool()
async def remove_from_cart(item_id: str, quantity: int = 1) -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.remove_from_cart(item_id, quantity)
    return f.getvalue()


@mcp.tool()
async def check_cart() -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        content = await ctx.order.get_cart_items()
    return f.getvalue() + "\nCart Details:\n" + str(content)


@mcp.tool()
async def checkout() -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.place_order()
    return f.getvalue()


@mcp.tool()
async def get_addresses() -> str:
    await ctx.ensure_started()
    addresses = await ctx.order.get_saved_addresses()
    if not addresses:
        return "No addresses found or Address Modal is not open. Try 'checkout' first."

    out = "Saved Addresses:\n"
    for addr in addresses:
        out += f"[{addr['index']}] {addr['label']} - {addr['details']}\n"
    return out


@mcp.tool()
async def select_address(index: int) -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.select_address(index)
    return f.getvalue()


@mcp.tool()
async def proceed_to_pay() -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.place_order()
    return f.getvalue()


@mcp.tool()
async def get_upi_ids() -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        ids = await ctx.order.get_upi_ids()
        if not ids:
            print("No UPI IDs found.")
        else:
            print("Available UPI IDs:")
            for i in ids:
                print(f"- {i}")
    return f.getvalue()


@mcp.tool()
async def select_upi_id(upi_id: str) -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.select_upi_id(upi_id)
    return f.getvalue()


@mcp.tool()
async def pay_now() -> str:
    await ctx.ensure_started()
    f = io.StringIO()
    with redirect_stdout(f):
        await ctx.order.click_pay_now()
    return f.getvalue()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    mcp.run()