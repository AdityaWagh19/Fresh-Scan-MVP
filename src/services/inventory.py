import logging
import pymongo
from datetime import datetime
from typing import List, Dict, Optional
from src.database.connection import DatabaseStateMachine
from src.services.vision import VisionService
from src.auth.profile import UserProfileManager
from src.utils.helpers import log_error

# Import mixins from other modules
from src.services.grocery import GroceryListManagerMixin
from src.services.integrations.blinkit import BlinkitOrderingMixin

# Import integration status
from src.services.integrations.blinkit import BLINKIT_AVAILABLE

# ===== From src/inventory/inventory_crud.py =====
import re
import pymongo
from datetime import datetime
from typing import List, Dict, Optional
from src.database.connection import DatabaseConnectionContext
from src.database.transactions import MongoTransaction, TransactionManager
from src.utils.helpers import log_error

def validate_item_name(name: str) -> Optional[str]:
    """
    Validate and sanitize item name. (Fix for Issue 1)
    Returns sanitized name or None if invalid.
    """
    if not name or not isinstance(name, str):
        return None
    
    # Strip whitespace
    name = name.strip()
    
    # Check length (max 100 characters)
    if len(name) > 100:
        return None
    
    # Allow only alphanumeric, spaces, hyphens, apostrophes
    if not re.match(r"^[a-zA-Z0-9\s\-']+$", name):
        return None
    
    return name

class InventoryCRUDMixin:
    """Core inventory management methods (CRUD)."""
    
    def _extract_quantity(self, quantity_str: str) -> float:
        """Extract numerical quantity from string (e.g., '2 bottles' -> 2.0)."""
        if not quantity_str:
            return 1.0
            
        try:
            # Extract first number from string
            match = re.search(r'(\d+\.?\d*)', str(quantity_str))
            return float(match.group(1)) if match else 1.0
        except:
            return 1.0

    def _compute_inventory_diff(self, old_inventory: List[Dict], new_inventory: List[Dict]) -> Dict:
        """Compute the difference between old and new inventories."""
        diff = {
            'added': [],
            'removed': [],
            'changed': [],
            'unchanged': []
        }
        
        # Create normalized dictionaries for comparison
        old_items = {(item['name'].lower(), item['category'].lower()): item for item in old_inventory}
        new_items = {(item['name'].lower(), item['category'].lower()): item for item in new_inventory}
        
        # Find added items
        for key in set(new_items.keys()) - set(old_items.keys()):
            diff['added'].append(new_items[key])
        
        # Find removed items
        for key in set(old_items.keys()) - set(new_items.keys()):
            diff['removed'].append(old_items[key])
        
        # Compare quantities of existing items
        for key in set(old_items.keys()) & set(new_items.keys()):
            old_item = old_items[key]
            new_item = new_items[key]
            
            # Try to extract numerical quantities if possible
            old_qty = self._extract_quantity(old_item.get('quantity', '1'))
            new_qty = self._extract_quantity(new_item.get('quantity', '1'))
            
            if old_qty != new_qty:
                changed_item = new_item.copy()
                changed_item['quantity_diff'] = new_qty - old_qty
                diff['changed'].append(changed_item)
            else:
                diff['unchanged'].append(new_item)
        
        return diff

    def _upsert_inventory_item(self, db, username: str, item: dict) -> dict:
        """
        Upsert inventory item using proper update logic. (Fix for Issue 2)
        """
        validated_name = validate_item_name(item['name'])
        validated_category = validate_item_name(item.get('category', 'Other'))
        
        if not validated_name or not validated_category:
            raise ValueError(f"Invalid item data: {item.get('name')} in {item.get('category')}")
        
        timestamp = datetime.now()
        
        # Use exact match with case-insensitive collation for lookup
        # We use $elemMatch to find the specific item in the array
        id_filter = {"_id": self.user_mgr.current_user['_id']}
        existing = db['users_v2'].find_one({**id_filter, "inventory": {"$elemMatch": {"name": {"$regex": f"^{re.escape(validated_name)}$", "$options": "i"}, "category": {"$regex": f"^{re.escape(validated_category)}$", "$options": "i"}}}})
        
        if existing:
            result = db['users_v2'].update_one({**id_filter, "inventory": {"$elemMatch": {"name": {"$regex": f"^{re.escape(validated_name)}$", "$options": "i"}, "category": {"$regex": f"^{re.escape(validated_category)}$", "$options": "i"}}}}, {"$set": {"inventory.$.quantity": item.get('quantity', 1), "inventory.$.notes": item.get('notes', ''), "inventory.$.last_seen": timestamp, "inventory.$.source": item.get('source', 'scan')}})
            return {"action": "updated", "modified": result.modified_count}
        else:
            new_entry = {"name": validated_name, "category": validated_category, "quantity": item.get('quantity', 1), "notes": item.get('notes', ''), "last_seen": timestamp, "source": item.get('source', 'scan'), "added_date": timestamp}
            result = db['users_v2'].update_one(id_filter, {"$push": {"inventory": new_entry}})
            return {"action": "inserted", "modified": result.modified_count}

    def save_items(self, new_items: List[Dict]) -> Dict[str, int]:
        """Save inventory items with differential updates and behavioral tracking using transactions."""
        if not new_items:
            return {"inserted": 0, "updated": 0, "removed": 0}
        
        try:
            if not self.user_mgr.current_user:
                print("\nNo user logged in")
                return {"inserted": 0, "updated": 0, "removed": 0}
            
            client = self.database.get_client()
            username = self.user_mgr.current_user['username']
            timestamp = datetime.now()
            
            # Get previous inventory (snapshot for comparison)
            old_inventory = self.get_current_inventory()
            
            # Compute differences
            diff = self._compute_inventory_diff(old_inventory, new_items)
            self._update_consumption_patterns(diff)
            
            # Update database using ACID transaction
            # Identify the new collection and user filter
            user_id = self.user_mgr.current_user.get('_id')
            id_filter = {"_id": user_id}

            def perform_save(txn: MongoTransaction):
                # Handle added/changed items
                for item in diff['added']:
                    try:
                        validated_name = validate_item_name(item['name'])
                        validated_category = validate_item_name(item.get('category', 'Other'))
                        
                        if not validated_name or not validated_category:
                            continue
                        
                        # Use a more efficient update (if exists update, else push)
                        query = id_filter.copy()
                        query["inventory"] = {
                            "$elemMatch": {
                                "name": {"$regex": f"^{re.escape(validated_name)}$", "$options": "i"},
                                "category": {"$regex": f"^{re.escape(validated_category)}$", "$options": "i"}
                            }
                        }
                        
                        # Check inside transaction
                        existing = txn.find_one("users_v2", query)
                        
                        if existing:
                            # Update existing
                            txn.update_one(
                                "users_v2",
                                query,
                                {
                                    "$set": {
                                        "inventory.$.quantity": item.get('quantity', 1),
                                        "inventory.$.notes": item.get('notes', ''),
                                        "inventory.$.last_seen": timestamp,
                                        "inventory.$.source": item.get('source', 'scan')
                                    }
                                }
                            )
                            result_counts["updated"] += 1
                        else:
                            # Push new
                            new_entry = {
                                "name": validated_name,
                                "category": validated_category,
                                "quantity": item.get('quantity', 1),
                                "notes": item.get('notes', ''),
                                "last_seen": timestamp,
                                "source": item.get('source', 'scan'),
                                "added_date": timestamp
                            }
                            txn.update_one(
                                "users_v2",
                                id_filter,
                                {"$push": {"inventory": new_entry}}
                            )
                            result_counts["inserted"] += 1
                            
                    except ValueError:
                        continue
                
                # Handle removed items
                for item in diff['removed']:
                    txn.update_one(
                        "users_v2",
                        id_filter,
                        {
                            "$pull": {
                                "inventory": {
                                    "name": item['name'],
                                    "category": item['category']
                                }
                            },
                            "$push": {
                                "consumption_history": {
                                    "item_name": item['name'],
                                    "category": item['category'],
                                    "action": "consumed",
                                    "timestamp": timestamp
                                }
                            }
                        }
                    )
                    result_counts["removed"] += 1
                    
                return result_counts

            # Execute the entire save operation as a single atomic unit
            return txn_mgr.execute_in_transaction(perform_save)
                
        except Exception as e:
            log_error("inventory save transaction", e)
            return {"inserted": 0, "updated": 0, "removed": 0}

    def get_current_inventory(self) -> List[Dict]:
        """Retrieve current inventory from database."""
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                if not self.user_mgr.current_user:
                    return []
                
                user_id = self.user_mgr.current_user.get('_id')
                user = db['users_v2'].find_one({"_id": user_id})
                
                if not user:
                    return []
                
                # Get items seen in the last 7 days
                from datetime import timedelta
                one_week_ago = datetime.now() - timedelta(days=7)
                inventory = user.get('inventory', [])
                current_items = [item for item in inventory
                               if item.get('last_seen', datetime.now()) >= one_week_ago]
                
                return sorted(current_items, key=lambda x: (x.get('category', ''), x.get('name', '')))
        except Exception as e:
            log_error("inventory fetch", e)
            return []

    def display_inventory(self) -> None:
        """Display current inventory in a formatted table."""
        items = self.get_current_inventory()
        if not items:
            print("\nNo items in inventory or database unavailable")
            return
        
        categories = {}
        for item in items:
            cat = item.get('category', 'Uncategorized')
            if cat not in categories: categories[cat] = []
            categories[cat].append(item)
        
        print("\nCurrent Refrigerator Inventory:")
        print("=" * 50)
        
        for category, cat_items in categories.items():
            print(f"\n{category.upper()}:")
            print("-" * len(category))
            for item in cat_items:
                name = item.get('name', 'Unknown')
                quantity = str(item.get('quantity', ''))
                last_seen = item.get('last_seen', datetime.now())
                last_seen_str = last_seen.strftime("%Y-%m-%d") if isinstance(last_seen, datetime) else str(last_seen)
                notes = item.get('notes', '')
                
                if self.user_mgr.current_user:
                    user_allergies = self.user_mgr.current_profile.get('allergies', [])
                    if user_allergies and self.vision_service.check_allergy_risk(name, user_allergies):
                        name = f" {name}"
                
                print(f"{name.ljust(25)} {quantity.ljust(15)} {last_seen_str.ljust(15)} {notes}")

    def scan_fridge(self, camera_service) -> bool:
        """Scan fridge contents using camera and AI analysis."""
        print("\nScanning Fridge Contents")
        capture_result = camera_service.capture_image()
        if not capture_result: return False
       
        image_path, metadata = camera_service.get_latest_image()
        if not image_path: 
            print(f"\nFailed to retrieve image after {metadata.get('attempts', 0)} attempts")
            return False
           
        processed_image = camera_service.preprocess_image(image_path)
        inventory_text = self.vision_service.analyze_inventory(processed_image)
        if not inventory_text: return False
           
        items = self.vision_service.parse_inventory(inventory_text)
        result = self.save_items(items)
       
        print(f"\nScan complete! Added {result['inserted']} new items, updated {result['updated']} items, and removed {result['removed']} items.")
        print("\nDetected Items:")
        print(inventory_text)
        return True

    def remove_item(self) -> None:
        """Remove an item from inventory."""
        items = self.get_current_inventory()
        if not items:
            print("\nNo items in inventory")
            return
       
        print("\nRemove Item from Inventory")
        for i, item in enumerate(items, 1):
            print(f"{i}. {item.get('name', 'Unknown')} ({item.get('category', 'Uncategorized')})")
        print("0. Cancel")
       
        try:
            choice = int(input("\nEnter item number: "))
            if choice == 0: return
               
            if 1 <= choice <= len(items):
                selected_item = items[choice-1]
                user_id = self.user_mgr.current_user['_id']
                id_filter = {"_id": user_id}
                
                # Initialize transaction manager
                txn_mgr = TransactionManager(self.database.get_client())

                def do_remove(txn: MongoTransaction):
                    txn.update_one(
                        "users_v2",
                        id_filter,
                        {"$pull": {"inventory": {"name": selected_item['name'], "category": selected_item['category']}}}
                    )
                    
                    txn.insert_one("session_logs", {
                        "action": "item_removed",
                        "timestamp": datetime.now(),
                        "username": self.user_mgr.current_user['username'],
                        "item_name": selected_item.get('name', 'Unknown'),
                        "category": selected_item.get('category', 'Uncategorized')
                    })
                
                txn_mgr.execute_in_transaction(do_remove)
                print(f"\n Removed {selected_item.get('name', 'item')} from inventory")
            else:
                print("\nInvalid selection")
        except Exception as e:
            log_error("item removal", e)

    def add_item_manually(self) -> None:
        """Add an item to inventory manually with auto-categorization."""
        print("\n" + "="*50)
        print("+ ADD ITEM MANUALLY")
        print("="*50)
        
        try:
            if not self.user_mgr.current_user:
                print("\n No user logged in")
                return
            
            name = input("\nItem name: ").strip()
            if not name: return
            
            print(f"\n Auto-categorizing '{name}' using AI...")
            category = self._auto_categorize_ingredient(name)
            print(f"Detected category: {category}")
            
            override = input(f"\nCategory detected as '{category}'. Change it? (y/n): ").strip().lower()
            if override == 'y':
                categories = ["Fruits", "Vegetables", "Dairy", "Meat", "Beverages", "Condiments", "Leftovers", "Grains", "Spices", "Snacks", "Frozen Foods"]
                for i, cat in enumerate(categories, 1): print(f"{i}. {cat}")
                cat_choice = input("\nEnter category number or custom category: ").strip()
                try: category = categories[int(cat_choice)-1]
                except (ValueError, IndexError): category = cat_choice if cat_choice else category
            
            quantity = input("\nQuantity (e.g., 2, half, 500g) [press Enter to skip]: ").strip()
            notes = input("Notes (optional): ").strip()
            
            normalized_name = self.vision_service.normalize_ingredient_name(name)
            item = {
                "name": normalized_name,
                "category": category,
                "quantity": quantity if quantity else "1",
                "notes": notes,
                "source": "manual"
            }
            
            txn_mgr = TransactionManager(self.database.get_client())
            username = self.user_mgr.current_user['username']
            
            user_id = self.user_mgr.current_user['_id']
            id_filter = {"_id": user_id}

            def do_add(txn: MongoTransaction):
                # Check for existing using users_v2 logic
                query = id_filter.copy()
                query["inventory"] = {
                    "$elemMatch": {
                        "name": {"$regex": f"^{re.escape(normalized_name)}$", "$options": "i"},
                        "category": {"$regex": f"^{re.escape(category)}$", "$options": "i"}
                    }
                }
                
                existing = txn.find_one("users_v2", query)
                
                if existing:
                    txn.update_one(
                        "users_v2",
                        query,
                        {
                            "$set": {
                                "inventory.$.quantity": quantity if quantity else "1",
                                "inventory.$.notes": notes,
                                "inventory.$.last_seen": datetime.now(),
                                "inventory.$.source": "manual"
                            }
                        }
                    )
                    action = "updated"
                else:
                    new_entry = {
                        "name": normalized_name,
                        "category": category,
                        "quantity": quantity if quantity else "1",
                        "notes": notes,
                        "last_seen": datetime.now(),
                        "source": "manual",
                        "added_date": datetime.now()
                    }
                    txn.update_one(
                        "users_v2",
                        id_filter,
                        {"$push": {"inventory": new_entry}}
                    )
                    action = "added"
                
                txn.insert_one("session_logs", {
                    "action": "item_added_manually",
                    "timestamp": datetime.now(),
                    "username": self.user_mgr.current_user['username'],
                    "item_name": normalized_name,
                    "category": category,
                    "result": action
                })
                return action

            res_action = txn_mgr.execute_in_transaction(do_add)
            if res_action == 'updated':
                print(f"\n Updated existing item: {normalized_name}")
            else:
                print(f"\n Added {normalized_name} to inventory")
        except Exception as e:
            log_error("manual item addition", e)

    def _auto_categorize_ingredient(self, ingredient_name: str) -> str:
        """Auto-categorize an ingredient using Gemini AI."""
        try:
            import google.generativeai as genai
            from src.config.constants import GEMINI_API_KEY, GEMINI_TEXT_MODEL
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            
            prompt = f"Categorize this ingredient into ONE category only: Fruits | Vegetables | Dairy | Meat | Beverages | Condiments | Grains | Spices | Snacks | Frozen Foods | Leftovers. Ingredient: {ingredient_name}. Return only the category name."
            response = model.generate_content(prompt)
            
            # Use safe extraction logic
            category = ""
            try:
                category = response.text
            except (ValueError, AttributeError):
                if hasattr(response, 'candidates') and response.candidates:
                    parts = []
                    for candidate in response.candidates:
                        if hasattr(candidate, 'content'):
                            for part in candidate.content.parts:
                                if hasattr(part, 'text'):
                                    parts.append(part.text)
                    category = "".join(parts)
            
            category = category.strip().strip('"\'.,\n').split('\n')[0]
            
            valid_categories = ["Fruits", "Vegetables", "Dairy", "Meat", "Beverages", "Condiments", "Grains", "Spices", "Snacks", "Frozen Foods", "Leftovers"]
            for valid_cat in valid_categories:
                if category.lower() == valid_cat.lower() or valid_cat.lower() in category.lower():
                    return valid_cat
            return self._fallback_categorize(ingredient_name)
        except:
            return self._fallback_categorize(ingredient_name)

    def _fallback_categorize(self, ingredient_name: str) -> str:
        """Fallback categorization using keyword matching."""
        ingredient_lower = ingredient_name.lower()
        category_keywords = {
            "Meat": ["chicken", "beef", "pork", "lamb", "mutton", "turkey", "duck", "fish", "salmon", "tuna", "cod", "shrimp", "prawn", "crab", "lobster", "bacon", "sausage", "ham", "steak", "mince"],
            "Vegetables": ["tomato", "potato", "onion", "garlic", "carrot", "broccoli", "spinach", "lettuce", "cabbage", "pepper", "capsicum", "cucumber", "celery", "cauliflower", "peas", "beans", "eggplant", "aubergine", "zucchini", "courgette", "squash", "pumpkin", "beetroot", "radish"],
            "Fruits": ["apple", "banana", "orange", "mango", "grape", "strawberry", "berry", "lemon", "lime", "watermelon", "melon", "pear", "peach", "plum", "cherry", "kiwi", "pineapple", "papaya", "guava"],
            "Dairy": ["milk", "cheese", "yogurt", "yoghurt", "butter", "cream", "paneer", "curd", "ghee", "cottage", "mozzarella", "cheddar"],
            "Grains": ["rice", "wheat", "bread", "pasta", "noodles", "oats", "quinoa", "barley", "flour", "cereal", "roti", "chapati", "couscous", "maida"],
            "Beverages": ["juice", "soda", "water", "tea", "coffee", "drink", "cola", "beer", "wine", "smoothie", "shake", "lassi", "milk shake"],
            "Condiments": ["sauce", "ketchup", "mayo", "mayonnaise", "mustard", "vinegar", "oil", "salt", "soy sauce", "chutney", "pickle"],
            "Spices": ["cumin", "turmeric", "coriander", "chili", "chilli", "garam", "masala", "curry", "cinnamon", "cardamom", "clove", "nutmeg", "pepper", "paprika", "oregano", "basil", "thyme"],
            "Snacks": ["chips", "cookies", "biscuit", "crackers", "nuts", "popcorn", "namkeen", "bhujia", "sev", "wafers"],
            "Frozen Foods": ["frozen", "ice cream", "popsicle", "ice", "gelato"]
        }
        for category, keywords in category_keywords.items():
            if any(keyword in ingredient_lower for keyword in keywords): return category
        return "Other"

    def edit_item(self) -> None:
        """Edit an existing inventory item with auto-categorization."""
        items = self.get_current_inventory()
        if not items:
            print("\nNo items in inventory")
            return
       
        print("\nEdit Inventory Item")
        for i, item in enumerate(items, 1):
            print(f"{i}. {item.get('name', 'Unknown')} ({item.get('category', 'Uncategorized')})")
        print("0. Cancel")
       
        try:
            choice = int(input("\nEnter item number: "))
            if choice == 0: return
               
            if 1 <= choice <= len(items):
                selected_item = items[choice-1]
                print(f"\nEditing: {selected_item.get('name')}")
                print("(Press Enter to keep current value)")
               
                name = input(f"Name [{selected_item.get('name', '')}]: ").strip()
                
                # Auto-detect category if name is changed
                auto_category = None
                if name:
                    print(f"\n Auto-categorizing '{name}' using AI...")
                    auto_category = self._auto_categorize_ingredient(name)
                    print(f"Detected category: {auto_category}")
                
                category = input(f"Category [{auto_category if auto_category else selected_item.get('category', '')}]: ").strip()
                quantity = input(f"Quantity [{selected_item.get('quantity', '')}]: ").strip()
                notes = input(f"Notes [{selected_item.get('notes', '')}]: ").strip()
               
                updates = {}
                if name: 
                    updates["name"] = self.vision_service.normalize_ingredient_name(name)
                if category: 
                    updates["category"] = category
                elif auto_category and name:
                    # Use auto-detected category if user didn't provide one
                    updates["category"] = auto_category
                if quantity: 
                    updates["quantity"] = quantity
                if notes: 
                    updates["notes"] = notes
               
                if updates:
                    user_id = self.user_mgr.current_user['_id']
                    id_filter = {"_id": user_id}
                    
                    # Initialize transaction manager
                    txn_mgr = TransactionManager(self.database.get_client())

                    def do_edit(txn: MongoTransaction):
                        # Construct a single update document for efficiency
                        update_doc = {}
                        for field, value in updates.items():
                            update_doc[f"inventory.$.{field}"] = value
                        
                        # Match doc filter
                        match_filter = id_filter.copy()
                        match_filter["inventory.name"] = selected_item['name']
                        match_filter["inventory.category"] = selected_item['category']

                        txn.update_one(
                            "users_v2",
                            match_filter,
                            {"$set": update_doc}
                        )
                        
                        txn.insert_one("session_logs", {
                            "action": "item_edited",
                            "timestamp": datetime.now(),
                            "username": self.user_mgr.current_user['username'],
                            "item_name": selected_item["name"],
                            "changes": updates
                        })
                    
                    txn_mgr.execute_in_transaction(do_edit)
                    print(f"\n Updated {selected_item.get('name', 'item')}")
                else:
                    print("\nNo changes made")
            else:
                print("\nInvalid selection")
        except Exception as e:
            log_error("item edit", e)


# ===== From src/inventory/consumption_tracker.py =====
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

def calculate_consumption_rate(history: list) -> Optional[float]:
    """
    Calculate consumption rate with robust error handling. (Fix for Issue 3)
    Returns: Consumption rate (items per day) or None if insufficient data
    """
    if not history or len(history) < 2:
        return None
    
    # Calculate time differences
    time_diffs = []
    # Sort history by timestamp just in case
    sorted_history = sorted(history, key=lambda x: x['timestamp'])
    
    for i in range(1, len(sorted_history)):
        delta = (sorted_history[i]['timestamp'] - sorted_history[i-1]['timestamp']).total_seconds()
        # Filter out very small time differences (< 60 seconds) to avoid data noise
        if delta >= 60:
            time_diffs.append(delta)
    
    if not time_diffs:
        return None
    
    try:
        avg_seconds_between = sum(time_diffs) / len(time_diffs)
        avg_hours_between = avg_seconds_between / 3600
        
        # Minimum threshold: 0.1 hours (6 minutes) to prevent division by zero or tiny numbers
        if avg_hours_between < 0.1:
            logger.warning(f"Average time between consumptions too small: {avg_hours_between}h")
            return None
        
        consumption_rate = 24 / avg_hours_between
        
        # Sanity check: max 100 items per day
        if consumption_rate > 100:
            logger.warning(f"Unrealistic consumption rate: {consumption_rate} items/day")
            return None
        
        return consumption_rate
    except ZeroDivisionError:
        return None
    except Exception as e:
        logger.error(f"Error calculating consumption rate: {e}")
        return None

class ConsumptionTrackerMixin:
    """Methods for tracking consumption patterns and generating smart recommendations."""

    def _update_consumption_patterns(self, diff: Dict) -> None:
        """Update consumption patterns based on inventory differences."""
        timestamp = datetime.now()
        
        # Track removed items as consumed
        for item in diff['removed']:
            item_name = item['name'].lower()
            if item_name not in self.consumption_patterns:
                self.consumption_patterns[item_name] = {
                    'last_consumed': timestamp,
                    'consumption_rate': None,
                    'history': []
                }
            
            self.consumption_patterns[item_name]['history'].append({
                'timestamp': timestamp,
                'action': 'consumed',
                'quantity': item.get('quantity', '1')
            })
            self.consumption_patterns[item_name]['last_consumed'] = timestamp
        
        # Track quantity changes
        for item in diff['changed']:
            if item.get('quantity_diff', 0) < 0:
                item_name = item['name'].lower()
                if item_name not in self.consumption_patterns:
                    self.consumption_patterns[item_name] = {
                        'last_consumed': timestamp,
                        'consumption_rate': None,
                        'history': []
                    }
                
                consumed_qty = abs(item['quantity_diff'])
                self.consumption_patterns[item_name]['history'].append({
                    'timestamp': timestamp,
                    'action': 'partial_consumed',
                    'quantity': consumed_qty
                })
                self.consumption_patterns[item_name]['last_consumed'] = timestamp
        
        # Recalculate consumption rates for all items using the fixed logic
        for item_name, pattern in self.consumption_patterns.items():
            pattern['consumption_rate'] = calculate_consumption_rate(pattern['history'])

    def _get_smart_recommendations(self, current_inventory, db, username):
        """Get smart recommendations based on patterns, user context, and cuisine needs."""
        recommendations = []
        threshold_date = datetime.now() - timedelta(days=7)
        
        context = self.user_mgr.current_profile or {}
        household_size = context.get('household_size', 1)
        dietary_restrictions = {
            'allergies': context.get('allergies', []),
            'diet_types': context.get('diet_types', [])
        }
        
        inventory_items = {(item['name'].lower(), item['category'].lower()): item 
                        for item in current_inventory}
        
        # === CONSUMPTION-BASED RECOMMENDATIONS ===
        # Get recently consumed items from DB
        consumed_items = list(db['users'].aggregate([
            {"$match": {"username": username}},
            {"$unwind": "$consumption_history"},
            {"$match": {
                "consumption_history.timestamp": {"$gte": threshold_date},
                "consumption_history.action": "consumed"
            }},
            {"$group": {
                "_id": {
                    "name": "$consumption_history.item_name",
                    "category": "$consumption_history.category"
                },
                "count": {"$sum": 1},
                "last_consumed": {"$max": "$consumption_history.timestamp"}
            }}
        ]))
        
        for item in consumed_items:
            item_name = item['_id']['name']
            item_key = (item_name.lower(), item['_id']['category'].lower())
            
            if self._violates_restrictions(item_name, dietary_restrictions):
                continue
            
            if item_key in inventory_items:
                inv_item = inventory_items[item_key]
                try:
                    current_qty = self._extract_quantity(inv_item.get('quantity', '1'))
                    if current_qty > 0.5: continue
                except: pass
            
            pattern = self.consumption_patterns.get(item_name.lower(), {})
            c_rate = pattern.get('consumption_rate') or 0
            last_date = item['last_consumed']
            days_since = (datetime.now() - last_date).days if last_date else 0
            
            if c_rate > 0.3:
                urgency, reason = "High", f"Frequently used ({c_rate:.1f}/day)"
            elif days_since < 3:
                urgency, reason = "Medium", "Recently consumed"
            else:
                urgency, reason = "Low", "Occasionally used"
            
            recommendations.append({
                "name": item_name,
                "category": item['_id']['category'],
                "reason": reason,
                "urgency": urgency,
                "quantity": 1 * household_size,
                "household_size": household_size,
                "last_consumed": last_date,
                "consumption_rate": c_rate,
                "source": "consumption_history"
            })
        
        # === CUISINE-BASED STAPLE RECOMMENDATIONS ===
        preferred_cuisines = context.get('preferred_cuisines', [])
        
        # Define cuisine staples
        cuisine_staples = {
            'Indian': [
                {'name': 'Onions', 'category': 'Vegetables', 'reason': 'Essential for Indian cooking'},
                {'name': 'Tomatoes', 'category': 'Vegetables', 'reason': 'Base for curries and gravies'},
                {'name': 'Ginger-Garlic Paste', 'category': 'Condiments', 'reason': 'Common aromatic base'},
                {'name': 'Green Chilies', 'category': 'Vegetables', 'reason': 'For spice and flavor'},
                {'name': 'Coriander Leaves', 'category': 'Herbs', 'reason': 'Garnish and flavor'},
                {'name': 'Yogurt', 'category': 'Dairy', 'reason': 'Used in marinades and curries'}
            ],
            'Italian': [
                {'name': 'Tomatoes', 'category': 'Vegetables', 'reason': 'Base for sauces'},
                {'name': 'Garlic', 'category': 'Vegetables', 'reason': 'Essential aromatic'},
                {'name': 'Basil', 'category': 'Herbs', 'reason': 'Classic Italian herb'},
                {'name': 'Parmesan Cheese', 'category': 'Dairy', 'reason': 'Common topping'}
            ],
            'Chinese': [
                {'name': 'Soy Sauce', 'category': 'Condiments', 'reason': 'Essential seasoning'},
                {'name': 'Ginger', 'category': 'Vegetables', 'reason': 'Key aromatic'},
                {'name': 'Garlic', 'category': 'Vegetables', 'reason': 'Key aromatic'},
                {'name': 'Scallions', 'category': 'Vegetables', 'reason': 'Garnish and flavor'}
            ],
            'Mexican': [
                {'name': 'Tomatoes', 'category': 'Vegetables', 'reason': 'Base for salsas'},
                {'name': 'Onions', 'category': 'Vegetables', 'reason': 'Essential aromatic'},
                {'name': 'Cilantro', 'category': 'Herbs', 'reason': 'Classic garnish'},
                {'name': 'Lime', 'category': 'Fruits', 'reason': 'For acidity and flavor'}
            ]
        }
        
        # Check for missing cuisine staples
        for cuisine in preferred_cuisines:
            if cuisine in cuisine_staples:
                for staple in cuisine_staples[cuisine]:
                    item_key = (staple['name'].lower(), staple['category'].lower())
                    
                    # Skip if violates dietary restrictions
                    if self._violates_restrictions(staple['name'], dietary_restrictions):
                        continue
                    
                    # Check if item is missing or low in inventory
                    is_missing = item_key not in inventory_items
                    is_low = False
                    
                    if not is_missing:
                        inv_item = inventory_items[item_key]
                        try:
                            current_qty = self._extract_quantity(inv_item.get('quantity', '1'))
                            is_low = current_qty < 0.3  # Less than 30% of a unit
                        except:
                            pass
                    
                    if is_missing or is_low:
                        # Check if already recommended from consumption history
                        already_recommended = any(
                            r['name'].lower() == staple['name'].lower() 
                            for r in recommendations
                        )
                        
                        if not already_recommended:
                            recommendations.append({
                                "name": staple['name'],
                                "category": staple['category'],
                                "reason": f"{staple['reason']} ({cuisine} cuisine)",
                                "urgency": "High" if is_missing else "Medium",
                                "quantity": 1 * household_size,
                                "household_size": household_size,
                                "source": "cuisine_staple"
                            })
        
        # Sort recommendations: High urgency first, then by consumption rate
        return sorted(recommendations, key=lambda x: (
            0 if x['urgency'] == 'High' else 1 if x['urgency'] == 'Medium' else 2,
            -(x.get('consumption_rate', 0))
        ))
    
    def _violates_restrictions(self, item_name: str, restrictions: Dict) -> bool:
        """Check if item violates dietary restrictions."""
        item_lower = item_name.lower()
        allergen_keywords = {
            'Dairy': ['milk', 'cheese', 'butter', 'cream', 'yogurt', 'cottage cheese', 'sour cream'],
            'Nuts': ['almond', 'peanut', 'cashew', 'walnut', 'hazelnut', 'pecan', 'pistachio', 'macadamia'],
            'Shellfish': ['shrimp', 'crab', 'lobster', 'prawn', 'scallop', 'oyster', 'mussel'],
            'Gluten': ['wheat', 'flour', 'bread', 'pasta', 'noodles', 'barley', 'rye'],
            'Eggs': ['egg', 'eggs', 'mayonnaise', 'custard'],
            'Soy': ['soy', 'tofu', 'soybean', 'miso', 'tempeh'],
            'Fish': ['fish', 'tuna', 'salmon', 'sardine', 'anchovy'],
            'Sesame': ['sesame', 'tahini']
        }
        
        for allergen in restrictions.get('allergies', []):
            keywords = allergen_keywords.get(allergen, [allergen.lower()])
            if any(kw in item_lower for kw in keywords): return True
        
        diet_types = restrictions.get('diet_types', [])
        if any(dt in ['Vegetarian', 'Vegan'] for dt in diet_types):
            if any(m in item_lower for m in ['chicken', 'beef', 'pork', 'fish', 'shrimp', 'meat', 'lamb', 'turkey', 'duck']):
                return True
        if 'Vegan' in diet_types:
            if any(d in item_lower for d in ['milk', 'cheese', 'butter', 'cream', 'yogurt', 'egg', 'eggs']):
                return True
        return False

# ===== InventoryManager (combines all mixins) =====

class InventoryManager(InventoryCRUDMixin, ConsumptionTrackerMixin, GroceryListManagerMixin, BlinkitOrderingMixin):
    """
    Manages fridge inventory operations by combining specialized mixins.
    Now refactored for better maintainability (Issues 1-5 fixed in mixins).
    """
    
    def __init__(self, database: DatabaseStateMachine, vision_service: VisionService, user_mgr: UserProfileManager):
        self.database = database
        self.vision_service = vision_service
        self.user_mgr = user_mgr
        self.consumption_patterns = {}  # Stores learned consumption patterns
        self.current_grocery_list = {
            "smart_recommendations": [],
            "selected_items": [],
            "custom_items": []
        }
        
        # Initialize logging
        self.logger = logging.getLogger(__name__)

    # Any methods that require tight coupling or orchestration between mixins can go here.
    # For now, most functionality is encapsulated in the mixins.

