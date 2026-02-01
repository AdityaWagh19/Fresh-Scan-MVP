"""
Hierarchical Menu System for Smart Fridge
Implements category-based navigation with progressive disclosure
"""

from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass
from enum import Enum


class MenuLevel(Enum):
    """Menu hierarchy levels"""
    MAIN = "main"
    CATEGORY = "category"
    ACTION = "action"


@dataclass
class MenuItem:
    """Represents a single menu item"""
    key: str
    label: str
    icon: str
    action: Optional[Callable] = None
    submenu: Optional['Menu'] = None
    description: str = ""


@dataclass
class Menu:
    """Represents a menu with items"""
    title: str
    items: List[MenuItem]
    level: MenuLevel
    parent: Optional['Menu'] = None


class NavigationState:
    """Tracks user's position in menu hierarchy"""
    
    def __init__(self):
        self.breadcrumbs: List[str] = []
        self.current_menu: Optional[Menu] = None
    
    def push(self, menu_title: str):
        """Navigate deeper into menu"""
        self.breadcrumbs.append(menu_title)
    
    def pop(self):
        """Navigate back one level"""
        if self.breadcrumbs:
            self.breadcrumbs.pop()
    
    def get_breadcrumb(self) -> str:
        """Get formatted breadcrumb trail"""
        if not self.breadcrumbs:
            return "Main Menu"
        return " > ".join(self.breadcrumbs)
    
    def reset(self):
        """Return to main menu"""
        self.breadcrumbs.clear()


class StatusBar:
    """Displays system status information"""
    
    def __init__(self, inventory_mgr, user_mgr):
        self.inventory_mgr = inventory_mgr
        self.user_mgr = user_mgr
        self.blinkit_status = "Unknown"
        self.cart_count = 0
    
    def update_blinkit_status(self, status: str):
        """Update Blinkit login status"""
        self.blinkit_status = status
    
    def update_cart_count(self, count: int):
        """Update cart item count"""
        self.cart_count = count
    
    def render(self) -> str:
        """Render status bar"""
        try:
            # Get inventory count
            inventory = self.inventory_mgr.get_current_inventory()
            item_count = len(inventory) if inventory else 0
            
            # Format cart status
            cart_status = f"{self.cart_count} items" if self.cart_count > 0 else "Empty"
            
            # Format Blinkit status with appropriate icon
            if self.blinkit_status == "Logged In":
                blinkit_icon = "OK"
            elif self.blinkit_status == "Unknown":
                blinkit_icon = "?"  # Neutral/not checked yet
            else:
                blinkit_icon = "ERROR"  # Error or logged out
            
            return f"Inventory: {item_count} items | Cart: {cart_status} | Blinkit: {self.blinkit_status}"
        except:
            return " Inventory |  Cart | Blinkit"


class MenuRenderer:
    """Handles menu display and formatting"""
    
    @staticmethod
    def render_header(title: str, breadcrumb: str = "", status: str = ""):
        """Render menu header with title and context"""
        print("\n" + "=" * 60)
        if breadcrumb and breadcrumb != "Main Menu":
            print(f"{breadcrumb}")
            print("=" * 60)
        print(f"           {title.upper()}")
        print("=" * 60)
        if status:
            print(status)
            print()
    
    @staticmethod
    def render_menu_items(items: List[MenuItem], show_back: bool = True):
        """Render menu items"""
        for item in items:
            if item.submenu:
                # Submenu indicator
                print(f"{item.key}. {item.icon} {item.label}")
            else:
                # Action item
                print(f"{item.key}. {item.icon} {item.label}")
        
        if show_back:
            print("0.  Back to Main Menu")
        else:
            print("0.  Logout")
        
        print("=" * 60)
    
    @staticmethod
    def render_footer():
        """Render menu footer"""
        pass
    
    @staticmethod
    def get_input(prompt: str = "Enter your choice") -> str:
        """Get user input with formatted prompt"""
        return input(f"\n{prompt}: ").strip()


class MenuSystem:
    """Main menu system controller"""
    
    def __init__(self, app):
        """Initialize menu system with app reference"""
        self.app = app
        self.nav_state = NavigationState()
        self.status_bar = StatusBar(app.inventory_mgr, app.user_mgr)
        self.renderer = MenuRenderer()
        self.verbose = app.debug
        
        # Build menu structure (parent references set after construction)
        self.main_menu = self._build_main_menu()
        self._set_parent_references()
    
    def _set_parent_references(self):
        """Set parent menu references after all menus are built"""
        # Set parents for category menus
        for item in self.main_menu.items:
            if item.submenu:
                item.submenu.parent = self.main_menu
                # Set parents for action menus (3rd level)
                for subitem in item.submenu.items:
                    if subitem.submenu:
                        subitem.submenu.parent = item.submenu
    
    def _build_main_menu(self) -> Menu:
        """
        Build the main menu structure.
        Phase 5-7: Journey-based, flattened hierarchy.
        """
        return Menu(
            title="SMART FRIDGE SYSTEM",
            level=MenuLevel.MAIN,
            items=[
                MenuItem(
                    key="1",
                    label="Scan Fridge",
                    icon="",
                    action=lambda: self.app.inventory_mgr.scan_fridge(self.app.camera_service)
                ),
                MenuItem(
                    key="2",
                    label="View Inventory",
                    icon="",
                    action=lambda: self.app.inventory_mgr.display_inventory()
                ),
                MenuItem(
                    key="3",
                    label="Manage Items",
                    icon="",
                    submenu=self._build_manage_items_menu()
                ),
                MenuItem(
                    key="4",
                    label="Get Recipes",
                    icon="",
                    submenu=self._build_get_recipes_menu()
                ),
                MenuItem(
                    key="5",
                    label="Shopping Lists",
                    icon="",
                    submenu=self._build_shopping_lists_menu()
                ),
                MenuItem(
                    key="6",
                    label="My Account",
                    icon="",
                    submenu=self._build_my_account_menu()
                ),
            ]
        )
    
    
    def _build_manage_items_menu(self) -> Menu:
        """
        Build manage items submenu.
        Phase 5-7: Flattened from 3 levels to 2.
        """
        return Menu(
            title="Manage Items",
            level=MenuLevel.CATEGORY,
            items=[
                MenuItem(
                    key="1",
                    label="Add Item Manually",
                    icon="",
                    action=lambda: self.app.inventory_mgr.add_item_manually()
                ),
                MenuItem(
                    key="2",
                    label="Edit Existing Item",
                    icon="",
                    action=lambda: self.app.inventory_mgr.edit_item()
                ),
                MenuItem(
                    key="3",
                    label="Remove Item",
                    icon="",
                    action=lambda: self.app.inventory_mgr.remove_item()
                ),
                MenuItem(
                    key="4",
                    label="View Consumption Patterns",
                    icon="",
                    action=lambda: self._show_consumption_patterns()
                ),
            ]
        )
    
    # Keep old inventory menu for backward compatibility (deprecated)
    def _build_inventory_menu(self) -> Menu:
        """Deprecated: Use _build_manage_items_menu() instead"""
        return self._build_manage_items_menu()
    
    def _build_inventory_edit_menu(self) -> Menu:
        """Deprecated: Merged into _build_manage_items_menu()"""
        return Menu(
            title="Edit Inventory",
            level=MenuLevel.ACTION,
            items=[
                MenuItem(
                    key="1",
                    label="Add item manually",
                    icon="",
                    action=lambda: self.app.inventory_mgr.add_item_manually()
                ),
                MenuItem(
                    key="2",
                    label="Update existing item",
                    icon="",
                    action=lambda: self.app.inventory_mgr.edit_item()
                ),
                MenuItem(
                    key="3",
                    label="Remove items",
                    icon="",
                    action=lambda: self.app.inventory_mgr.remove_item()
                ),
            ]
        )
    
    
    def _build_get_recipes_menu(self) -> Menu:
        """
        Build get recipes submenu.
        Phase 5-7: Renamed from recipes_menu.
        """
        return Menu(
            title="Get Recipes",
            level=MenuLevel.CATEGORY,
            items=[
                MenuItem(
                    key="1",
                    label="Suggest Recipes (AI)",
                    icon="",
                    action=lambda: self.app.recipe_mgr.suggest_recipes()
                ),
                MenuItem(
                    key="2",
                    label="View Saved Recipes",
                    icon="",
                    action=lambda: self.app.recipe_mgr.view_favorite_recipes()
                ),
                MenuItem(
                    key="3",
                    label="Search Custom Recipe",
                    icon="",
                    action=lambda: self.app.recipe_mgr._search_custom_recipe()
                ),
            ]
        )
    
    # Keep old recipes menu for backward compatibility (deprecated)
    def _build_recipes_menu(self) -> Menu:
        """Deprecated: Use _build_get_recipes_menu() instead"""
        return self._build_get_recipes_menu()
    
    
    def _build_shopping_lists_menu(self) -> Menu:
        """
        Build shopping lists submenu.
        Phase 5-7: Renamed from shopping_menu.
        """
        return Menu(
            title="Shopping Lists",
            level=MenuLevel.CATEGORY,
            items=[
                MenuItem(
                    key="1",
                    label="Create New List",
                    icon="",
                    action=lambda: self.app.inventory_mgr.generate_grocery_list()
                ),
                MenuItem(
                    key="2",
                    label="View Saved Lists",
                    icon="",
                    action=lambda: self.app.inventory_mgr.view_grocery_lists()
                ),
                MenuItem(
                    key="3",
                    label="Order via Blinkit",
                    icon="",
                    submenu=self._build_blinkit_menu() if self.app.health.integration == "ok" else None,
                    action=lambda: self._show_blinkit_unavailable() if self.app.health.integration != "ok" else None
                ),
            ]
        )
    
    # Keep old shopping menu for backward compatibility (deprecated)
    def _build_shopping_menu(self) -> Menu:
        """Deprecated: Use _build_shopping_lists_menu() instead"""
        return self._build_shopping_lists_menu()
    
    def _build_blinkit_menu(self) -> Menu:
        """
        Build Blinkit ordering submenu.
        Phase 5-7: Simplified, removed Quick Order placeholder.
        """
        return Menu(
            title="Order via Blinkit",
            level=MenuLevel.ACTION,
            items=[
                MenuItem(
                    key="1",
                    label="Order from Saved List",
                    icon="",
                    action=lambda: self.app.inventory_mgr.order_from_saved_list()
                ),
                MenuItem(
                    key="2",
                    label="Manage Blinkit Account",
                    icon="",
                    action=lambda: self.app._run_integration_menu()
                ),
            ]
        )
    
    
    
    def _build_my_account_menu(self) -> Menu:
        """
        Build my account submenu.
        Phase 5-7: Consolidates profile and settings.
        """
        return Menu(
            title="My Account",
            level=MenuLevel.CATEGORY,
            items=[
                MenuItem(
                    key="1",
                    label="View Profile",
                    icon="",
                    action=lambda: self.app.user_mgr.view_profile()
                ),
                MenuItem(
                    key="2",
                    label="Edit Profile",
                    icon="",
                    action=lambda: self.app.user_mgr.edit_profile()
                ),
                MenuItem(
                    key="3",
                    label="System Status",
                    icon="",
                    action=lambda: self.app.display_system_status()
                ),
                MenuItem(
                    key="4",
                    label="Activity Logs",
                    icon="",
                    action=lambda: self._view_logs()
                ),
            ]
        )
    
    # Keep old menus for backward compatibility (deprecated)
    def _build_profile_menu(self) -> Menu:
        """Deprecated: Use _build_my_account_menu() instead"""
        return Menu(
            title="My Profile",
            level=MenuLevel.CATEGORY,
            items=[
                MenuItem(
                    key="1",
                    label="View Full Profile",
                    icon="",
                    action=lambda: self.app.user_mgr.view_profile()
                ),
                MenuItem(
                    key="2",
                    label="Edit Profile",
                    icon="",
                    action=lambda: self.app.user_mgr.edit_profile()
                ),
            ]
        )
    
    def _build_settings_menu(self) -> Menu:
        """Deprecated: Merged into _build_my_account_menu()"""
        return Menu(
            title="System Settings",
            level=MenuLevel.CATEGORY,
            items=[
                MenuItem(
                    key="1",
                    label="Check System Status",
                    icon="",
                    action=lambda: self.app.display_system_status()
                ),
                MenuItem(
                    key="2",
                    label="View Activity Logs",
                    icon="",
                    action=lambda: self._view_logs()
                ),
            ]
        )
    
    
    def _show_consumption_patterns(self):
        """Show consumption patterns (placeholder)"""
        print("\n Consumption Patterns")
        print("=" * 60)
        print("This feature analyzes your usage trends.")
        print("Coming soon: Predictive restocking recommendations!")
        input("\nPress Enter to continue...")
    
    def _quick_order(self):
        """Quick order feature (placeholder)"""
        print("\n Quick Order")
        print("=" * 60)
        print("Enter items to order directly without creating a list.")
        print("This feature is under development.")
        input("\nPress Enter to continue...")
    
    def _show_blinkit_unavailable(self):
        """Show Blinkit unavailable message"""
        print("\nERROR: Blinkit integration is not available")
        print("   Reason: Integration module failed to load")
        print("   Please check that the integration/ folder exists")
        input("\nPress Enter to continue...")
    
    def _view_logs(self):
        """View activity logs"""
        print("\n Activity Logs")
        print("=" * 60)
        print("Recent activity:")
        try:
            with open('smart_fridge.log', 'r') as f:
                lines = f.readlines()
                for line in lines[-20:]:  # Last 20 lines
                    print(line.strip())
        except FileNotFoundError:
            print("No logs available yet.")
        input("\nPress Enter to continue...")
    
    def display_menu(self, menu: Menu):
        """Display a menu and handle navigation"""
        # Update status bar
        status = self.status_bar.render()
        
        # Show user info for main menu
        user_info = ""
        if menu.level == MenuLevel.MAIN and self.app.user_mgr.current_user:
            email = self.app.user_mgr.current_user.get('email', 'User')
            user_info = f"Welcome, {email}\n{status}"
        
        # Render menu
        breadcrumb = self.nav_state.get_breadcrumb()
        self.renderer.render_header(menu.title, breadcrumb, user_info)
        
        # Add profile summary for profile menu
        if menu.title == "My Profile" and self.app.user_mgr.current_profile:
            profile = self.app.user_mgr.current_profile
            print(f"User: {self.app.user_mgr.current_user.get('email', 'N/A')}")
            print(f"Household: {profile.get('household_size', 'N/A')} people | "
                  f"Diet: {', '.join(profile.get('diet_types', ['N/A']))} | "
                  f"Budget: {profile.get('grocery_budget', 'N/A')}")
            print()
        
        self.renderer.render_menu_items(menu.items, show_back=(menu.level != MenuLevel.MAIN))
        
        return menu
    
    def run(self) -> bool:
        """Run the menu system. Returns False if user wants to logout."""
        current_menu = self.main_menu
        self.nav_state.reset()
        
        while True:
            try:
                # Display current menu
                displayed_menu = self.display_menu(current_menu)
                
                # Get user choice
                choice = self.renderer.get_input()
                
                # Handle back/logout
                if choice == '0':
                    if current_menu.level == MenuLevel.MAIN:
                        # Logout from main menu
                        return False
                    else:
                        # Go back one level
                        if current_menu.parent:
                            current_menu = current_menu.parent
                            self.nav_state.pop()
                        else:
                            current_menu = self.main_menu
                            self.nav_state.reset()
                        continue
                
                # Find selected item
                selected_item = None
                for item in displayed_menu.items:
                    if item.key == choice:
                        selected_item = item
                        break
                
                if not selected_item:
                    print("\nERROR: Invalid choice. Please try again.")
                    input("Press Enter to continue...")
                    continue
                
                # Execute action or navigate to submenu
                if selected_item.submenu:
                    # Navigate to submenu
                    self.nav_state.push(selected_item.label)
                    current_menu = selected_item.submenu
                elif selected_item.action:
                    # Execute action
                    try:
                        selected_item.action()
                    except KeyboardInterrupt:
                        print("\n\nWARNING: Operation cancelled by user")
                        input("Press Enter to continue...")
                    except Exception as e:
                        print(f"\nERROR: Error: {e}")
                        if self.verbose:
                            import traceback
                            traceback.print_exc()
                        input("Press Enter to continue...")
                else:
                    print("\nWARNING: This feature is not yet implemented.")
                    input("Press Enter to continue...")
            
            except KeyboardInterrupt:
                print("\n\nWARNING: Interrupted. Returning to menu...")
                continue
            except Exception as e:
                print(f"\nERROR: Menu error: {e}")
                if self.verbose:
                    import traceback
                    traceback.print_exc()
                input("Press Enter to continue...")
                continue
