import logging
from datetime import datetime
from typing import List, Dict, Optional
from src.database.connection import DatabaseConnectionContext
from src.database.transactions import MongoTransaction, TransactionManager, VersionConflictError
from src.utils.helpers import log_error

logger = logging.getLogger(__name__)

class GroceryListVersionConflict(Exception):
    """Raised when grocery list version conflict occurs. (Fix for Issue 5)"""
    pass

class GroceryListManagerMixin:
    """Methods for managing grocery lists with optimistic locking."""

    def _update_grocery_list_with_locking(self, txn: MongoTransaction, username: str, list_name: str, updates: dict, expected_version: int = None):
        """Update grocery list with optimistic locking using transactions."""
        query = {"username": username, "name": list_name}
        if expected_version is not None:
            query["version"] = expected_version
        
        # Increment version
        updates["version"] = (expected_version or 0) + 1
        updates["updated_at"] = datetime.now()
        
        result = txn.update_one("grocery_lists", query, {"$set": updates})
        
        if result.matched_count == 0:
            if expected_version is not None:
                # Check for conflict inside same transaction
                exists = txn.find_one("grocery_lists", {"username": username, "name": list_name})
                if exists:
                    raise VersionConflictError(f"Version mismatch for '{list_name}'. Expected {expected_version}, found {exists.get('version')}")
            raise ValueError(f"Grocery list '{list_name}' not found or modified by another process")
        
        return True

    def view_grocery_lists(self) -> None:
        """View and manage all saved grocery lists."""
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                if not self.user_mgr.current_user:
                    print("\nPlease log in to view grocery lists")
                    return
                
                username = self.user_mgr.current_user['username']
                while True:
                    saved_lists = list(db['grocery_lists'].find({"username": username}).sort("created_at", -1))
                    if not saved_lists:
                        print("\n No saved grocery lists found.")
                        return
                    
                    self._display_saved_grocery_lists(saved_lists)
                    choice = input("\nEnter your choice (0-5): ").strip()
                    
                    if choice == '0': break
                    elif choice == '1': self._view_grocery_list_details(saved_lists, db)
                    elif choice == '2': self._edit_existing_grocery_list(saved_lists, db, username)
                    elif choice == '3': self._delete_grocery_list(saved_lists, db)
                    elif choice == '4': self._export_grocery_list(saved_lists)
                    elif choice == '5': self._compare_grocery_lists(saved_lists)
                    else: print("\nInvalid choice.")
        except Exception as e:
            log_error("view grocery lists", e)

    def _display_saved_grocery_lists(self, saved_lists):
        print("\n" + "="*60 + "\n YOUR SAVED GROCERY LISTS\n" + "="*60)
        for i, gl in enumerate(saved_lists, 1):
            created = gl['created_at'].strftime('%Y-%m-%d %H:%M')
            count = gl.get('total_items', len(gl.get('items', [])))
            version = gl.get('version', 0)
            print(f"{i}. {'' if gl.get('type') == 'enhanced' else ''} {gl['name']} (v{version})")
            print(f"{created} |  {count} items")
        print("\n 1:View | 2:Edit | 3:Delete | 4:Export | 5:Compare | 0:Back")

    def _view_grocery_list_details(self, saved_lists, db):
        try:
            num = int(input(f"\nEnter list number (1-{len(saved_lists)}): "))
            if 1 <= num <= len(saved_lists):
                gl = saved_lists[num - 1]
                print(f"\n {gl['name'].upper()}\n" + "="*60)
                print(f"Created: {gl['created_at'].strftime('%Y-%m-%d %H:%M')}")
                print(f"Items: {len(gl.get('items', []))} | v{gl.get('version', 0)}")
                
                items = gl.get('items', [])
                cats = {}
                for it in items:
                    c = it.get('category', 'Other')
                    if c not in cats: cats[c] = []
                    cats[c].append(it)
                
                for c, its in cats.items():
                    print(f"\n  {c.upper()}")
                    for it in its:
                        icon = "" if it.get('type') == 'smart' else "" if it.get('type') == 'custom' else ""
                        qty = f" ({it['quantity']})" if it.get('quantity') else ""
                        print(f"{icon} {it['name']}{qty}")
                input("\nPress Enter to continue...")
        except: print("Invalid selection.")

    def _edit_existing_grocery_list(self, saved_lists, db, username):
        try:
            num = int(input(f"\nEnter list number to edit (1-{len(saved_lists)}): "))
            if 1 <= num <= len(saved_lists):
                gl = saved_lists[num - 1]
                self.current_grocery_list = {
                    "smart_recommendations": [], "selected_items": [], "custom_items": [],
                    "_existing_list_id": gl['_id'], "_existing_version": gl.get('version', 0)
                }
                for it in gl.get('items', []):
                    if it.get('type') == 'custom': self.current_grocery_list["custom_items"].append(it)
                    else: self.current_grocery_list["selected_items"].append(it)
                
                self.current_grocery_list["smart_recommendations"] = self._get_smart_recommendations(self.get_current_inventory(), db, username)
                self._continue_grocery_list_editing(db, username, gl['name'])
        except: print("Invalid selection.")

    def _continue_grocery_list_editing(self, db, username, list_name):
        common_items = {
            "Daily Needs": ["Milk", "Bread", "Eggs", "Rice", "Cooking Oil", "Sugar", "Salt"],
            "Fruits": ["Bananas", "Apples", "Oranges", "Grapes", "Mangoes", "Pomegranates", "Seasonal Fruits"],
            "Vegetables": ["Tomatoes", "Onions", "Potatoes", "Green Chilies", "Coriander Leaves", "Ginger-Garlic", "Mixed Vegetables"]
        }
        while True:
            self._display_grocery_menu(common_items)
            choice = input("\nEnter choice: ").strip()
            if choice == '1': self._browse_and_add_common_items(common_items)
            elif choice == '2': self._add_custom_item()
            elif choice == '3': self._remove_items_from_list()
            elif choice == '4': self._view_full_grocery_list()
            elif choice == '5': self._add_smart_recommendations()
            elif choice == '6':
                if self._update_existing_grocery_list_internal(db, username, list_name): break
            elif choice == '0': break

    def _update_existing_grocery_list_internal(self, db, username, original_name):
        all_items = self.current_grocery_list["selected_items"] + self.current_grocery_list["custom_items"]
        if not all_items: return False
        
        new_name = input(f"New name (Enter for '{original_name}'): ").strip() or original_name
        
        # Check if this is a new list or an existing one
        is_new_list = '_existing_list_id' not in self.current_grocery_list
        
        try:
            txn_mgr = TransactionManager(self.database.get_client())
            
            def perform_update(txn: MongoTransaction):
                if is_new_list:
                    # Check if name already exists for this user
                    if txn.find_one("grocery_lists", {"username": username, "name": new_name}):
                        raise ValueError(f"A grocery list named '{new_name}' already exists.")
                        
                    doc = {
                        "username": username, 
                        "name": new_name, 
                        "items": all_items,
                        "created_at": datetime.now(), 
                        "type": "enhanced", 
                        "total_items": len(all_items), 
                        "version": 0
                    }
                    txn.insert_one("grocery_lists", doc)
                    return ("inserted", 0)
                else:
                    version = self.current_grocery_list.get('_existing_version', 0)
                    self._update_grocery_list_with_locking(txn, username, original_name, {
                        "name": new_name, "items": all_items, "total_items": len(all_items)
                    }, expected_version=version)
                    return ("updated", version + 1)

            action, new_v = txn_mgr.execute_in_transaction(perform_update)
            
            if action == "inserted":
                print(f"Saved '{new_name}' (new list)")
            else:
                print(f"Updated '{new_name}' (v{new_v})")
            
            # Blinkit integration check
            self._handle_blinkit_offer(all_items)
            return True
        except VersionConflictError as e:
            print(f"Conflict: {e}. Another process updated this list. Please reload.")
            return False
        except Exception as e:
            print(f"Error: {e}")
            return False

    def _handle_blinkit_offer(self, all_items):
        from src.services.integrations.blinkit import BLINKIT_AVAILABLE
        if BLINKIT_AVAILABLE:
            print("\n" + "="*60 + "\n BLINKIT ORDERING\n" + "="*60)
            if input("\nWould you like to order these items on Blinkit now? (y/n): ").strip().lower() == 'y':
                print("\n Starting Blinkit ordering process...\n This may take a few moments...")
                try:
                    from src.services.integrations.blinkit import order_grocery_list
                    result = order_grocery_list(all_items, auto_checkout=True)
                    
                    if result.get('success'):
                        print("\n" + "="*60 + "\n ORDERING COMPLETE\n" + "="*60)
                        print(result.get('message', ''))
                        if result.get('added_count', 0) > 0:
                            print(f"\n Summary:\n  - Items added to cart: {result.get('added_count')}/{result.get('total_items')}")
                            if result.get('failed_items'):
                                print(f"\n  Some items could not be added:")
                                for failed in result.get('failed_items', []):
                                    print(f"- {failed.get('item')}: {failed.get('reason')}")
                            print("\n PAYMENT REQUIRED\n" + "="*60)
                            print("Your cart is ready on Blinkit!\n Please complete payment on the Blinkit website or app.")
                    else:
                        print("\n" + "="*60 + "\n ORDERING FAILED\n" + "="*60)
                        print(result.get('message', 'Unknown error occurred'))
                        if result.get('requires_login'):
                            print("\n TIP: Use option 12 from the main menu to login to Blinkit first.")
                except Exception as e:
                    log_error("Blinkit ordering", e)
                    print(f"\n Error during Blinkit ordering: {str(e)}")

    def _delete_grocery_list(self, saved_lists, db):
        try:
            num = int(input(f"\nEnter list number to delete: "))
            if 1 <= num <= len(saved_lists):
                gl = saved_lists[num - 1]
                if input(f"Type 'DELETE' to confirm {gl['name']}: ") == 'DELETE':
                    db['grocery_lists'].delete_one({"_id": gl['_id']})
                    print("Deleted.")
        except: pass

    def _export_grocery_list(self, saved_lists):
        try:
            num = int(input(f"Export list number: "))
            gl = saved_lists[num - 1]
            fname = f"grocery_{gl['name'].replace(' ', '_').lower()}.txt"
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(f" {gl['name'].upper()}\nItems: {len(gl['items'])}\n\n")
                for it in gl['items']: f.write(f"   {it['name']} ({it.get('quantity', '1')})\n")
            print(f"Exported to {fname}")
        except: pass

    def _compare_grocery_lists(self, saved_lists):
        try:
            i1 = int(input("First list: ")) - 1
            i2 = int(input("Second list: ")) - 1
            l1, l2 = saved_lists[i1], saved_lists[i2]
            s1 = {it['name'].lower() for it in l1['items']}
            s2 = {it['name'].lower() for it in l2['items']}
            print(f"\nCommon: {', '.join(s1 & s2)}")
            print(f"Only in {l1['name']}: {', '.join(s1 - s2)}")
            print(f"Only in {l2['name']}: {', '.join(s2 - s1)}")
        except: pass

    def generate_grocery_list(self) -> None:
        try:
            with DatabaseConnectionContext(self.database.get_client()) as db:
                if not self.user_mgr.current_user: return
                username = self.user_mgr.current_user['username']
                recent = list(db['grocery_lists'].find({"username": username}).sort("created_at", -1).limit(3))
                
                if recent:
                    print("\n" + "="*60)
                    print(" GROCERY LIST OPTIONS")
                    print("="*60)
                    print(f"You have {len(recent)} saved grocery list(s)")
                    print("\n1.  Create New Grocery List")
                    print("2.   Edit Existing Grocery List")
                    print("0.  Back to Menu")
                    print("="*60)
                    
                    choice = input("\nEnter your choice (0-2): ").strip()
                    
                    if choice == '2':
                        self._edit_existing_grocery_list(recent, db, username)
                        return
                    elif choice == '0':
                        return
                    elif choice != '1':
                        print("Invalid choice. Creating new list...")
                
                # Create new list
                self.current_grocery_list = {
                    "smart_recommendations": self._get_smart_recommendations(self.get_current_inventory(), db, username),
                    "selected_items": [], "custom_items": []
                }
                self._continue_grocery_list_editing(db, username, "Quick list")
        except Exception as e: log_error("generate list", e)


    def _save_grocery_list(self, db, username):
        all_items = self.current_grocery_list["selected_items"] + self.current_grocery_list["custom_items"]
        if not all_items: return False
        name = input("List name (Enter for auto): ").strip() or f"List {datetime.now().strftime('%m-%d %H:%M')}"
        
        doc = {
            "username": username, "name": name, "items": all_items,
            "created_at": datetime.now(), "type": "enhanced", 
            "total_items": len(all_items), "version": 0
        }
        db['grocery_lists'].insert_one(doc)
        print(f"Saved '{name}'")
        self._handle_blinkit_offer(all_items)
        return True

    # Helper UI methods
    def _display_grocery_menu(self, common):
        total = len(self.current_grocery_list["selected_items"]) + len(self.current_grocery_list["custom_items"])
        print(f"\n GROCERY MANAGER | Items: {total}")
        print("1:Common | 2:Custom | 3:Remove | 4:View | 5:Smart | 6:Save | 0:Exit")

    def _browse_and_add_common_items(self, common):
        cats = list(common.keys())
        for i, c in enumerate(cats, 1): print(f"{i}. {c}")
        try:
            num = int(input("Select category: "))
            if 1 <= num <= len(cats): self._add_items_from_category(common[cats[num-1]], cats[num-1])
        except: pass

    def _add_items_from_category(self, items, cat):
        for i, it in enumerate(items, 1):
            status = "OK" if self._is_item_in_list(it, cat) else " "
            print(f"{status} {i}. {it}")
        choice = input("Enter numbers (e.g. 1,2) or 0: ").strip()
        if choice != '0':
            try:
                for n in [int(x) for x in choice.split(',')]:
                    if 1 <= n <= len(items):
                        name = items[n-1]
                        if self._is_item_in_list(name, cat): 
                            self._remove_item_from_list(name, cat)
                        else: 
                            self.current_grocery_list["selected_items"].append({"name": name, "category": cat, "type": "common"})
            except: pass

    def _add_custom_item(self):
        name = input("Custom item name: ").strip()
        if name:
            self.current_grocery_list["custom_items"].append({"name": name, "category": "Custom", "type": "custom"})

    def _remove_items_from_list(self):
        all_it = self.current_grocery_list["selected_items"] + self.current_grocery_list["custom_items"]
        for i, it in enumerate(all_it, 1): print(f"{i}. {it['name']}")
        try:
            nums = sorted([int(x) for x in input("Remove numbers: ").split(',')], reverse=True)
            for n in nums:
                if 1 <= n <= len(all_it):
                    idx = n - 1
                    if idx < len(self.current_grocery_list["selected_items"]): self.current_grocery_list["selected_items"].pop(idx)
                    else: self.current_grocery_list["custom_items"].pop(idx - len(self.current_grocery_list["selected_items"]))
        except: pass

    def _view_full_grocery_list(self):
        all_it = self.current_grocery_list["selected_items"] + self.current_grocery_list["custom_items"]
        if not all_it: print("Empty.")
        else:
            for it in all_it: print(f"- {it['name']} ({it.get('category')})")
        input("\nEnter to continue...")

    def _add_smart_recommendations(self):
        recs = self.current_grocery_list["smart_recommendations"]
        if not recs: print("No recommendations.")
        else:
            for i, r in enumerate(recs, 1): print(f"{i}. {r['name']} ({r['urgency']}) - {r['reason']}")
            try:
                nums = [int(x) for x in input("Add numbers: ").split(',')]
                for n in nums:
                    if 1 <= n <= len(recs):
                        r = recs[n-1]
                        self.current_grocery_list["selected_items"].append({"name": r['name'], "category": r['category'], "type": "smart"})
            except: pass

    def _is_item_in_list(self, name, cat):
        all_it = self.current_grocery_list["selected_items"] + self.current_grocery_list["custom_items"]
        return any(i['name'].lower() == name.lower() for i in all_it)

    def _remove_item_from_list(self, name, cat):
        self.current_grocery_list["selected_items"] = [i for i in self.current_grocery_list["selected_items"] if i['name'].lower() != name.lower()]
        self.current_grocery_list["custom_items"] = [i for i in self.current_grocery_list["custom_items"] if i['name'].lower() != name.lower()]
