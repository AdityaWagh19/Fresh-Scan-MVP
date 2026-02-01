from typing import Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.inventory import InventoryManager

from src.auth.profile import UserProfileManager


class UserContextManager:
    """Manages unified user context for all modules."""
    
    def __init__(self, user_mgr: UserProfileManager, inventory_mgr: 'InventoryManager'):
        self.user_mgr = user_mgr
        self.inventory_mgr = inventory_mgr
    
    def get_unified_context(self) -> Dict:
        """Returns complete context for all modules."""
        # Get user profile data
        profile = self.user_mgr.current_profile or {}
        
        return {
            'household_size': profile.get('household_size', 1),
            'dietary_restrictions': {
                'allergies': profile.get('allergies', []),
                'diet_types': profile.get('diet_types', []),
                'cultural_restrictions': profile.get('cultural_restrictions', [])
            },
            'cuisine_preferences': profile.get('cuisine_preferences', []),
            'meal_frequency': profile.get('meal_frequency', 3),
            'preferred_proteins': profile.get('preferred_proteins', []),
            'budget': profile.get('budget', 'medium'),
            'current_inventory': self.inventory_mgr.get_current_inventory(),
            'consumption_patterns': self.inventory_mgr.consumption_patterns
        }
