import random
import hashlib
import logging
from datetime import datetime
from typing import List, Dict, Set, Optional, Any
from src.database.connection import DatabaseStateMachine, DatabaseConnectionContext, ConnectionStatus, DatabaseConnectionError
from src.auth.profile import UserProfileManager
from src.services.camera import CameraService
from src.services.vision import VisionService
from src.services.inventory import InventoryManager
from src.config.constants import MONGO_URI, CACHE_DIR
from src.utils.helpers import log_error
import pymongo

# YouTube integration
from src.services.integrations.youtube import YouTubeQuotaManager, fetch_youtube_videos_with_quota

logger = logging.getLogger(__name__)

class RecipeManager:
    """Enhanced Recipe Manager with unique and diverse recipe generation."""
    
    def __init__(self, database, inventory_mgr, vision_service, user_mgr, context_mgr=None):
        self.database = database
        self.inventory_mgr = inventory_mgr
        self.vision_service = vision_service
        self.user_mgr = user_mgr
        self.context_mgr = context_mgr
        self.generated_recipes = set()  # Track generated recipes to avoid duplicates
        self.session_recipes = []  # Store recipes for current session
        
        # Initialize pantry staples dictionary
        self.pantry_staples = {
            'oils': ['oil', 'olive oil', 'vegetable oil', 'sesame oil', 'coconut oil'],
            'spices': ['salt', 'pepper', 'black pepper', 'cumin', 'turmeric', 'chili', 'paprika', 'cinnamon'],
            'aromatics': ['garlic', 'ginger', 'onion', 'shallot'],
            'herbs': ['basil', 'oregano', 'thyme', 'rosemary', 'cilantro', 'parsley', 'mint'],
            'condiments': ['soy sauce', 'vinegar', 'balsamic vinegar', 'mustard', 'ketchup'],
            'sweeteners': ['sugar', 'brown sugar', 'honey', 'maple syrup'],
            'acids': ['lemon juice', 'lime juice', 'vinegar']
        }
        
        # Initialize YouTube quota manager
        self.youtube_quota_mgr = YouTubeQuotaManager(self.database)
        
        # Initialize YouTube service
        self.youtube_service = None
        self._setup_youtube()

    def _setup_youtube(self) -> None:
        """Initialize YouTube API service."""
        try:
            from src.config.constants import YOUTUBE_API_KEY
            
            if YOUTUBE_API_KEY and YOUTUBE_API_KEY != "YOUR_YOUTUBE_API_KEY":
                from googleapiclient.discovery import build
                self.youtube_service = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
                print("YouTube API configured successfully!")
            else:
                print("YouTube API key not configured. Video links will be unavailable.")
                self.youtube_service = None
        except ImportError:
            print("google-api-python-client not installed. Run: pip install google-api-python-client")
            self.youtube_service = None
        except Exception as e:
            log_error("YouTube API setup", e)
            self.youtube_service = None

    def _fetch_youtube_videos(self, recipe_name: str) -> List[Dict[str, str]]:
        """Search and return YouTube videos for a recipe with quota management and caching.
        
        Returns:
            List of dicts with 'title', 'url', and 'thumbnail' keys (max 3)
        """
        try:
            if not self.youtube_service:
                return []
            
            videos = fetch_youtube_videos_with_quota(
                self.youtube_service,
                recipe_name,
                self.youtube_quota_mgr,
                max_results=3  # Explicit cap
            )
            
            return videos[:3]  # Enforce cap before return
            
        except Exception as e:
            log_error("YouTube video search", e)
            return []

    def suggest_recipes(self, num_recipes: int = 3) -> List[Dict]:
        """Generate diverse recipe suggestions using AI with full user context."""
        try:
            print("\n [*] Analyzing your fridge contents...")
            available_ingredients = self._get_available_ingredients()
            
            if not available_ingredients:
                print("ERROR: No ingredients available for recipe suggestions.")
                print("INFO: Try adding some ingredients to your inventory first!")
                return []
            
            # Get unified context
            context = self.context_mgr.get_unified_context() if self.context_mgr else {}
            
            # Filter ingredients by dietary restrictions
            dietary_restrictions = context.get('dietary_restrictions', {})
            safe_ingredients = self._filter_by_dietary_restrictions(
                available_ingredients,
                dietary_restrictions
            )
            
            if not safe_ingredients:
                print("ERROR: No safe ingredients available after applying dietary restrictions.")
                print("INFO: Try adding more ingredients that match your dietary preferences!")
                return []
            
            print(f"OK: Found ingredients: {', '.join(safe_ingredients)}")
            
            # Check if AI service is available
            if not self.vision_service or not self.vision_service.text_model:
                print("\nERROR: AI service unavailable. Cannot generate recipes.")
                print("INFO: Please check your internet connection or AI configuration.")
                return []
            
            # Extract full user context
            inventory_text = ", ".join(safe_ingredients)
            
            # Get cuisine preference
            cuisine_preferences = context.get('cuisine_preferences', [])
            if isinstance(cuisine_preferences, list) and cuisine_preferences:
                cuisine = cuisine_preferences[0]
            else:
                cuisine = context.get('cuisine_preference', 'International')
                if isinstance(cuisine, list) and cuisine:
                    cuisine = cuisine[0]
                cuisine_preferences = [cuisine]
            
            # Extract other context parameters
            allergies = context.get('allergies', [])
            cultural_restrictions = context.get('cultural_restrictions', [])
            age_groups = context.get('age_groups', [])
            budget = context.get('budget', 'Medium')
            household_size = context.get('household_size', 2)
            
            # Format dietary restrictions
            diet_types = dietary_restrictions.get('diet_types', [])
            dietary_str = ", ".join(diet_types) if diet_types else "None"
            
            print(f"\n🤖 Generating {num_recipes} AI-powered recipes...")
            if allergies:
                print(f"WARNING:  Avoiding allergens: {', '.join(allergies)}")
            if cultural_restrictions:
                print(f"🕌 Respecting restrictions: {', '.join(cultural_restrictions)}")
            
            # Generate recipes using AI with full context
            try:
                recipe_text = self.vision_service.generate_recipes(
                    inventory_text=inventory_text,
                    cuisine=cuisine,
                    num_recipes=num_recipes,
                    dietary_restrictions=dietary_str,
                    allergies=allergies,
                    cultural_restrictions=cultural_restrictions,
                    age_groups=age_groups,
                    budget=budget,
                    cuisine_preferences=cuisine_preferences,
                    household_size=household_size
                )
                
                if not recipe_text:
                    print("\nERROR: AI failed to generate recipes. Please try again.")
                    print("INFO: Tip: Try adding more ingredients or simplifying your restrictions.")
                    return []
                
                recipes = self.vision_service.parse_recipes(recipe_text)
                
                if not recipes:
                    print("\nERROR: Failed to parse AI response. Please try again.")
                    return []
                
                # Add metadata to AI recipes
                for r in recipes:
                    r['hash'] = hashlib.md5(r['name'].encode()).hexdigest()
                    r['type'] = 'ai_generated'
                    if not r.get('servings'):
                        r['servings'] = str(household_size)
                
                # Store in session
                for recipe in recipes:
                    if recipe not in self.session_recipes:
                        self.session_recipes.append(recipe)
                
                # Display recipes with enhanced formatting
                print(f"\n✨ Recipe Suggestions ({len(recipes)} recipes):")
                print("=" * 60)
                for i, recipe in enumerate(recipes, 1):
                    print(f"\n{i}. 🍽️  {recipe['name']}")
                    if recipe.get('description'):
                        print(f"    {recipe['description']}")
                    if recipe.get('cuisine'):
                        print(f"   🌍 Cuisine: {recipe['cuisine']}")
                    if recipe.get('cooking_time') or recipe.get('total_time'):
                        time_str = recipe.get('total_time', recipe.get('cooking_time', ''))
                        print(f"   ⏱️  Time: {time_str}")
                    if recipe.get('difficulty'):
                        print(f"    Difficulty: {recipe['difficulty']}")
                    
                    # Show missing ingredients count (NEW)
                    missing_count = len(recipe.get('missing_ingredients', []))
                    if missing_count > 0:
                        print(f"    Missing Items: {missing_count} ingredient(s) to purchase")
                    else:
                        print(f"   OK: All ingredients available!")
                
                print("\n" + "=" * 60)
                
                # Validate ingredient diversity
                self._validate_ingredient_diversity(recipes, safe_ingredients)
                
                # Show options
                self._show_incremental_recipe_options()
                
                return recipes
                
            except Exception as ai_err:
                log_error("AI recipe generation", ai_err)
                print(f"\nERROR: Error during AI recipe generation: {ai_err}")
                print("INFO: Please try again or contact support if the issue persists.")
                return []
            
        except Exception as e:
            log_error("recipe generation", e)
            print(f"\nERROR: Error generating recipes: {e}")
            return []

    def _validate_ingredient_diversity(self, recipes: List[Dict], available_ingredients: List[str]) -> None:
        """
        Validate that recipes use diverse ingredient subsets and provide feedback.
        
        Args:
            recipes: List of generated recipes
            available_ingredients: List of all available ingredients
        """
        if not recipes or not available_ingredients:
            return
        
        total_available = len(available_ingredients)
        warnings = []
        
        # Track ingredient usage across recipes
        ingredient_usage_count = {}
        for recipe in recipes:
            ingredients = recipe.get('ingredients', [])
            selected = recipe.get('ingredients_selected', [])
            not_used = recipe.get('ingredients_not_used', [])
            
            # Count how many ingredients each recipe uses
            num_ingredients = len(ingredients)
            
            # Check if recipe is using too many ingredients (>6 is suspicious)
            if num_ingredients > 6:
                warnings.append(
                    f"WARNING:  '{recipe['name']}' uses {num_ingredients} ingredients (recommended: 3-6)"
                )
            
            # Track which ingredients are being used across all recipes
            for ing in ingredients:
                ingredient_usage_count[ing] = ingredient_usage_count.get(ing, 0) + 1
        
        # Check if certain ingredients are overused (appear in all recipes)
        overused_ingredients = [
            ing for ing, count in ingredient_usage_count.items() 
            if count == len(recipes) and len(recipes) > 1
        ]
        
        if overused_ingredients and len(overused_ingredients) > 2:
            warnings.append(
                f"WARNING:  Some ingredients appear in ALL recipes: {', '.join(overused_ingredients[:3])}"
            )
        
        # Check for diversity: Each recipe should use different main ingredients
        main_ingredients_sets = []
        for recipe in recipes:
            ingredients = recipe.get('ingredients', [])
            if ingredients:
                # Consider first 2-3 ingredients as "main" ingredients
                main_ingredients_sets.append(set(ingredients[:3]))
        
        # Calculate diversity score (how different are the main ingredients?)
        if len(main_ingredients_sets) > 1:
            diversity_scores = []
            for i in range(len(main_ingredients_sets)):
                for j in range(i + 1, len(main_ingredients_sets)):
                    intersection = main_ingredients_sets[i] & main_ingredients_sets[j]
                    union = main_ingredients_sets[i] | main_ingredients_sets[j]
                    if union:
                        similarity = len(intersection) / len(union)
                        diversity_scores.append(1 - similarity)
            
            if diversity_scores:
                avg_diversity = sum(diversity_scores) / len(diversity_scores)
                if avg_diversity < 0.5:  # Less than 50% diverse
                    warnings.append(
                        "WARNING:  Recipes seem similar - consider requesting more diverse options"
                    )
        
        # Display validation results
        if warnings:
            print("\n" + "━" * 60)
            print(" INGREDIENT DIVERSITY ANALYSIS")
            print("━" * 60)
            for warning in warnings:
                print(warning)
            print("\nINFO: Tip: For better variety, try generating recipes again or")
            print("   specify different cuisine preferences in your profile.")
            print("━" * 60)
        else:
            # Positive feedback
            print("\nOK: Great! Recipes show good ingredient diversity and complementary pairings.")
    
    def _show_incremental_recipe_options(self):
        """Show options after displaying initial recipes with incremental loading."""
        print("\n" + "="*60)
        print("WHAT WOULD YOU LIKE TO DO?")
        print("="*60)
        print("1. View detailed recipe with YouTube videos")
        print("2. Save a recipe to favorites")
        print("3.  Load 3 more recipe suggestions")
        print("4.  Search for a custom recipe")
        print("5. Filter recipes by cooking time")
        print("0. Return to main menu")
        
        choice = input("\nEnter your choice (0-5): ").strip()
        
        if choice == '1':
            self._show_recipe_details()
        elif choice == '2':
            self._save_to_favorites()
        elif choice == '3':
            self._load_more_recipes()
        elif choice == '4':
            self._search_custom_recipe()
        elif choice == '5':
            self._filter_by_cooking_time()
        elif choice == '0':
            return
        else:
            print("Invalid choice.")
    
    def _show_recipe_options(self):
        """Legacy method - redirects to incremental options."""
        self._show_incremental_recipe_options()

    def _show_recipe_details(self):
        """Show detailed instructions for a selected recipe."""
        if not self.session_recipes:
            print("No recipes available. Generate some recipes first!")
            self._show_incremental_recipe_options()
            return
        
        try:
            recipe_num = int(input(f"Enter recipe number (1-{len(self.session_recipes)}): ")) - 1
            if 0 <= recipe_num < len(self.session_recipes):
                recipe = self.session_recipes[recipe_num]
                self._display_detailed_recipe(recipe)
                # Note: _display_detailed_recipe now handles returning to options
            else:
                print("Invalid recipe number.")
                self._show_incremental_recipe_options()
        except ValueError:
            print("Please enter a valid number.")
            self._show_incremental_recipe_options()
        except Exception as e:
            log_error("show recipe details", e)
            print(f"\nERROR: Error: {e}")
            print("INFO: Returning to recipe menu...")
            self._show_incremental_recipe_options()


    def _display_detailed_recipe(self, recipe: Dict):
        """Display detailed recipe with instructions, missing ingredients, nutrition, AND YouTube videos."""
        try:
            print(f"\n🍽️  {recipe['name']}")
            print("=" * (len(recipe['name']) + 6))
            print(f" Description: {recipe.get('description', 'Delicious homemade dish')}")
            print(f"⏱️  Time: {recipe.get('total_time', recipe.get('cooking_time', '15-20'))}")
            print(f" Difficulty: {recipe.get('difficulty', 'Easy')}")
            print(f"👥 Servings: {recipe.get('servings', '2-3')} people")
            
            print("\n🥘 Ingredients:")
            for ingredient in recipe.get('ingredients', []):
                print(f"  - {ingredient}")
            
            # Display missing ingredients (shopping list) - NEW
            missing_ingredients = recipe.get('missing_ingredients', [])
            if missing_ingredients:
                print("\n" + "─" * 60)
                print(" MISSING INGREDIENTS (Shopping List)")
                print("─" * 60)
                for item in missing_ingredients:
                    print(f"  ERROR: {item}")
                print("\nINFO: Tip: Add these items to your grocery list before cooking!")
                print("─" * 60)
            
            # Display nutrition information - NEW
            nutrition = recipe.get('nutrition', {})
            if nutrition:
                print("\n" + "─" * 60)
                print("🥗 NUTRITION (per serving)")
                print("─" * 60)
                for nutrient, value in nutrition.items():
                    print(f"  - {nutrient.capitalize()}: {value}")
                print("─" * 60)
            
            # Display AI's ingredient selection reasoning (if available)
            ingredients_selected = recipe.get('ingredients_selected', [])
            ingredients_not_used = recipe.get('ingredients_not_used', [])
            
            if ingredients_selected or ingredients_not_used:
                print("\n" + "─" * 60)
                print("🤖 AI's Ingredient Selection Reasoning:")
                if ingredients_selected:
                    print(f"  OK: Selected: {', '.join(ingredients_selected[:5])}")
                    if len(ingredients_selected) > 5:
                        print(f"     ... and {len(ingredients_selected) - 5} more")
                if ingredients_not_used:
                    print(f"  ⊘ Not Used: {', '.join(ingredients_not_used[:5])}")
                    if len(ingredients_not_used) > 5:
                        print(f"     ... and {len(ingredients_not_used) - 5} more")
                print("─" * 60)
            
            # Display pantry staples used (assumed available)
            pantry_items_used = recipe.get('pantry_staples', [])
            if pantry_items_used:
                print("\n Pantry Staples Used (assumed available):")
                for item in pantry_items_used:
                    print(f"   - {item}")
                print("\nINFO: Note: These are common pantry items not tracked in your fridge inventory")
            
            print(f"\n👨‍ Instructions:")
            
            # Get instructions - handle both AI-generated and fallback
            instructions = recipe.get('instructions', [])
            if not instructions:
                try:
                    # Try to generate basic instructions as fallback
                    instructions = self._generate_basic_instructions(recipe)
                except Exception as e:
                    # If generation fails, show a helpful message
                    print(f"  ℹ️  Detailed instructions are being prepared...")
                    print(f"  INFO: Please refer to the ingredients list and YouTube videos below for guidance.")
                    instructions = []
            
            for i, step in enumerate(instructions, 1):
                print(f"  {i}. {step}")
            
            print(f"\nINFO: Tips:")
            tips = recipe.get('tips', [f"Adjust seasoning to taste", f"Serve hot for best flavor"])
            for tip in tips:
                print(f"  - {tip}")
            
            # Display YouTube videos
            print("\n" + "="*60)
            print("📺 YOUTUBE VIDEO TUTORIALS (Max 3)")
            print("="*60)
            
            if self.youtube_service:
                print(" Searching for video tutorials...")
                videos = self._fetch_youtube_videos(recipe['name'])
                
                # Enforce cap before display
                videos = videos[:3]
                
                if videos:
                    for i, video in enumerate(videos, 1):
                        print(f"\n{i}. 🎥 {video['title']}")
                        print(f"   🔗 {video['url']}")
                else:
                    print("ERROR: No video tutorials found for this recipe.")
            else:
                print("WARNING:  YouTube integration not available. Configure API key to enable video tutorials.")
            
            print("="*60)
            input("\n⏎ Press Enter to continue...")
            
            # After viewing details, return to recipe options
            self._show_incremental_recipe_options()
            
        except Exception as e:
            log_error("display detailed recipe", e)
            print(f"\nERROR: Error displaying recipe details: {e}")
            print("INFO: Returning to recipe menu...")
            input("\n⏎ Press Enter to continue...")
            # Still return to recipe options even on error
            self._show_incremental_recipe_options()



    def _generate_basic_instructions(self, recipe: Dict) -> List[str]:
        """Generate basic cooking instructions for a recipe."""
        recipe_type = recipe.get('type', 'stir_fry')
        pantry_used = []
        
        instructions = {
            'noodles': [
                "Cook noodles according to package directions, drain and set aside",
                "Heat oil in a large pan or wok over medium-high heat",
                "Add garlic and ginger, stir-fry for 30 seconds until fragrant",
                "Add vegetables and cook for 2-3 minutes until tender-crisp",
                "Add cooked noodles and sauce, toss everything together",
                "Cook for 1-2 minutes until heated through and well combined",
                "Garnish with green onions or herbs and serve immediately"
            ],
            'rice': [
                "Cook rice according to package directions, let cool slightly",
                "Heat oil in a large pan or wok over high heat",
                "Add garlic and ginger, stir-fry for 30 seconds",
                "Add vegetables and cook until tender",
                "Add cold rice, breaking up any clumps",
                "Stir-fry for 3-4 minutes, adding sauce gradually",
                "Taste and adjust seasoning, serve hot"
            ],
            'stir_fry': [
                "Prepare all ingredients and have them ready (mise en place)",
                "Heat oil in a wok or large pan over high heat",
                "Add aromatics (garlic, ginger) and stir-fry for 30 seconds",
                "Add protein if using, cook until nearly done",
                "Add vegetables in order of cooking time (hardest first)",
                "Add sauce and toss everything together quickly",
                "Cook for 1-2 minutes until vegetables are tender-crisp"
            ]
        }
        
        instruction_list = instructions.get(recipe_type, instructions['stir_fry'])
        
        # Track pantry staples mentioned in instructions
        instruction_text = " ".join(instruction_list).lower()
        for category, items in self.pantry_staples.items():
            for item in items:
                if item.lower() in instruction_text:
                    pantry_used.append(item)
        
        # Add pantry staples to recipe metadata
        recipe['pantry_staples'] = list(set(pantry_used))
        
        return instruction_list

    def _save_to_favorites(self):
        """Save a recipe to user favorites with proper validation."""
        if not self.session_recipes:
            print("ERROR: No recipes available to save!")
            self._show_incremental_recipe_options()
            return
        
        # Show available recipes
        print("\n Available recipes:")
        for i, recipe in enumerate(self.session_recipes, 1):
            print(f"{i}. {recipe['name']}")
        
        try:
            recipe_num = int(input(f"\nEnter recipe number to save (1-{len(self.session_recipes)}): ")) - 1
            
            # Validate index is within bounds
            if recipe_num < 0 or recipe_num >= len(self.session_recipes):
                print(f"ERROR: Invalid recipe number. Please enter a number between 1 and {len(self.session_recipes)}.")
                self._show_incremental_recipe_options()
                return
            
            recipe = self.session_recipes[recipe_num]
            
            # Save to database and check result
            success = self._save_recipe_to_db(recipe)
            
            if success:
                print(f"OK: '{recipe['name']}' saved to your favorites!")
            else:
                print(f"ERROR: Failed to save '{recipe['name']}'. Please try again.")
            
            # Return to recipe options
            self._show_incremental_recipe_options()
                
        except ValueError:
            print("ERROR: Please enter a valid number.")
            self._show_incremental_recipe_options()
        except Exception as e:
            log_error("save to favorites", e)
            print(f"ERROR: Error saving recipe: {e}")
            self._show_incremental_recipe_options()


    def _save_recipe_to_db(self, recipe: Dict) -> bool:
        """
        Save recipe to database with YouTube links.
        
        Returns:
            bool: True if save successful, False otherwise
        """
        try:
            # Get current user ID - use email as primary identifier
            user_id = None
            
            # Try multiple sources for user ID
            if self.user_mgr.current_user:
                # First try email (most reliable for OAuth)
                user_id = self.user_mgr.current_user.get('email')
                # Fallback to username if email not available
                if not user_id:
                    user_id = self.user_mgr.current_user.get('username')
            
            # Fallback to current_user_id attribute
            if not user_id:
                user_id = getattr(self.user_mgr, 'current_user_id', None)
            
            # Final fallback
            if not user_id:
                user_id = 'default_user'
                logger.warning("Could not determine user ID, using default_user")
            
            # Debug: Log the user ID being used
            logger.info(f"Saving recipe for user_id: {user_id}")
            
            # Fetch YouTube videos if not already present
            youtube_videos = recipe.get('youtube_videos', [])
            if not youtube_videos and self.youtube_service:
                youtube_videos = self._fetch_youtube_videos(recipe['name'])
            
            recipe_data = {
                'user_id': user_id,
                'name': recipe['name'],
                'type': recipe.get('type', 'generated'),
                'ingredients': recipe.get('ingredients', []),
                'instructions': recipe.get('instructions', []),
                'description': recipe.get('description', ''),
                'cooking_time': recipe.get('cooking_time', '15-20'),
                'difficulty': recipe.get('difficulty', 'Easy'),
                'youtube_videos': youtube_videos,
                'pantry_staples': recipe.get('pantry_staples', []),  # Store pantry staples
                'created_at': datetime.now()
            }
            
            # Insert into database
            with DatabaseConnectionContext(self.database.get_client()) as db:
                result = db['favorite_recipes'].insert_one(recipe_data)
                if result.inserted_id:
                    logger.info(f"Recipe '{recipe['name']}' saved successfully for user {user_id}")
                    return True
                else:
                    logger.error(f"Failed to save recipe '{recipe['name']}'")
                    return False
            
        except Exception as e:
            log_error("save recipe to database", e)
            logger.error(f"Error saving recipe to database: {e}")
            return False

    def _generate_fresh_recipes(self):
        """Generate completely new recipes by clearing previous ones."""
        self.generated_recipes.clear()
        self.session_recipes.clear()
        print("\n Generating fresh recipes...")
        self.suggest_recipes()

    def _filter_by_cooking_time(self):
        """Filter recipes by cooking time."""
        if not self.session_recipes:
            print("No recipes available to filter!")
            return
        
        print("\nFilter by cooking time:")
        print("1. Quick (under 15 minutes)")
        print("2. Medium (15-30 minutes)") 
        print("3. Long (over 30 minutes)")
        
        choice = input("Enter your choice (1-3): ")
        
        filtered_recipes = []
        if choice == '1':
            filtered_recipes = [r for r in self.session_recipes if 'quick' in r.get('cooking_time', '').lower() or '10' in r.get('cooking_time', '')]
        elif choice == '2':
            filtered_recipes = [r for r in self.session_recipes if '15' in r.get('cooking_time', '') or '20' in r.get('cooking_time', '')]
        elif choice == '3':
            filtered_recipes = [r for r in self.session_recipes if '30' in r.get('cooking_time', '') or 'slow' in r.get('cooking_time', '').lower()]
        
        if filtered_recipes:
            print(f"\n  Filtered Recipes ({len(filtered_recipes)} found):")
            for i, recipe in enumerate(filtered_recipes, 1):
                print(f"{i}. {recipe['name']} - {recipe.get('cooking_time', 'Unknown')} minutes")
        else:
            print("No recipes found matching your time criteria.")
    
    
    def _load_more_recipes(self):
        """Generate and display 3 additional recipes using AI."""
        print("\nProgress: Generating 3 more recipes...")
        
        # Get context for filtering
        context = self.context_mgr.get_unified_context() if self.context_mgr else {}
        available_ingredients = self._get_available_ingredients()
        
        if not available_ingredients:
            print("ERROR: No ingredients available.")
            return
        
        # Filter by dietary restrictions
        dietary_restrictions = context.get('dietary_restrictions', {})
        safe_ingredients = self._filter_by_dietary_restrictions(
            available_ingredients,
            dietary_restrictions
        )
        
        if not safe_ingredients:
            print("ERROR: No safe ingredients available after applying dietary restrictions.")
            return
        
        # Check if AI service is available
        if not self.vision_service or not self.vision_service.text_model:
            print("\nERROR: AI service unavailable. Cannot generate more recipes.")
            print("INFO: Please check your internet connection or AI configuration.")
            return
        
        # Extract full user context (same as suggest_recipes)
        inventory_text = ", ".join(safe_ingredients)
        
        # Get cuisine preference
        cuisine_preferences = context.get('cuisine_preferences', [])
        if isinstance(cuisine_preferences, list) and cuisine_preferences:
            cuisine = cuisine_preferences[0]
        else:
            cuisine = context.get('cuisine_preference', 'International')
            if isinstance(cuisine, list) and cuisine:
                cuisine = cuisine[0]
            cuisine_preferences = [cuisine]
        
        # Extract other context parameters
        allergies = context.get('allergies', [])
        cultural_restrictions = context.get('cultural_restrictions', [])
        age_groups = context.get('age_groups', [])
        budget = context.get('budget', 'Medium')
        household_size = context.get('household_size', 2)
        
        # Format dietary restrictions
        diet_types = dietary_restrictions.get('diet_types', [])
        dietary_str = ", ".join(diet_types) if diet_types else "None"
        
        print(f"🤖 Generating 3 more AI-powered recipes...")
        
        # Generate recipes using AI with full context
        try:
            recipe_text = self.vision_service.generate_recipes(
                inventory_text=inventory_text,
                cuisine=cuisine,
                num_recipes=3,
                dietary_restrictions=dietary_str,
                allergies=allergies,
                cultural_restrictions=cultural_restrictions,
                age_groups=age_groups,
                budget=budget,
                cuisine_preferences=cuisine_preferences,
                household_size=household_size
            )
            
            if not recipe_text:
                print("\nERROR: AI failed to generate more recipes. Please try again.")
                return
            
            new_recipes = self.vision_service.parse_recipes(recipe_text)
            
            if not new_recipes:
                print("\nERROR: Failed to parse AI response. Please try again.")
                return
            
            # Add metadata to AI recipes
            for r in new_recipes:
                r['hash'] = hashlib.md5(r['name'].encode()).hexdigest()
                r['type'] = 'ai_generated'
                if not r.get('servings'):
                    r['servings'] = str(household_size)
            
            # Add to session
            for recipe in new_recipes:
                self.session_recipes.append(recipe)
            
            # Display new recipes
            print(f"\n✨ Generated {len(new_recipes)} additional recipes!")
            print(f" Total recipes this session: {len(self.session_recipes)}")
            print("\n" + "=" * 60)
            
            for i, recipe in enumerate(new_recipes, 1):
                session_num = len(self.session_recipes) - len(new_recipes) + i
                print(f"\n{session_num}. 🍽️  {recipe['name']}")
                if recipe.get('description'):
                    print(f"    {recipe['description']}")
                if recipe.get('cuisine'):
                    print(f"   🌍 Cuisine: {recipe['cuisine']}")
                if recipe.get('cooking_time') or recipe.get('total_time'):
                    time_str = recipe.get('total_time', recipe.get('cooking_time', ''))
                    print(f"   ⏱️  Time: {time_str}")
                if recipe.get('difficulty'):
                    print(f"    Difficulty: {recipe['difficulty']}")
            
            print("\n" + "=" * 60)
            
            # Validate ingredient diversity for new recipes
            self._validate_ingredient_diversity(new_recipes, safe_ingredients)
            
            # Show options again
            self._show_incremental_recipe_options()
            
        except Exception as e:
            log_error("load more recipes", e)
            print(f"\nERROR: Error generating more recipes: {e}")
            print("INFO: Please try again or return to main menu.")

    
    def _search_custom_recipe(self):
        """Allow user to search for their own recipe and get YouTube tutorials."""
        print("\n" + "="*60)
        print("CUSTOM RECIPE SEARCH")
        print("="*60)
        print("Search for any recipe you'd like to cook!")
        
        recipe_query = input("\nEnter recipe name (e.g., 'Pasta Carbonara', 'Chicken Tikka'): ").strip()
        
        if not recipe_query:
            print("Recipe name cannot be empty.")
            return
        
        print(f"\n Searching for '{recipe_query}'...")
        
        # Fetch YouTube videos for the custom recipe
        if self.youtube_service:
            videos = self._fetch_youtube_videos(recipe_query)
            
            if videos:
                print(f"\n Found {len(videos)} video tutorials for '{recipe_query}':")
                print("="*60)
                for i, video in enumerate(videos, 1):
                    print(f"\n{i}. {video['title']}")
                    print(f"{video['url']}")
                print("="*60)
                
                # Option to save custom recipe
                save_choice = input("\n Would you like to save this recipe to favorites? (y/n): ").strip().lower()
                if save_choice == 'y':
                    self._save_custom_recipe_with_videos(recipe_query, videos)
            else:
                print(f"No video tutorials found for '{recipe_query}'.")
                print("Try a different recipe name or check your spelling.")
        else:
            print("YouTube integration not available. Configure API key to enable search.")
        
        input("\nPress Enter to continue...")
    
    def _save_custom_recipe_with_videos(self, recipe_name: str, videos: List[Dict]):
        """Save a custom-searched recipe with YouTube links."""
        try:
            if not self.user_mgr.current_user:
                print("Please log in to save recipes.")
                return
            
            custom_recipe = {
                'name': recipe_name,
                'type': 'custom_search',
                'ingredients': [],
                'instructions': [],
                'description': f'Custom recipe: {recipe_name}',
                'youtube_videos': videos,
                'created_at': datetime.now()
            }
            
            self._save_recipe_to_db(custom_recipe)
            print(f"Saved '{recipe_name}' with {len(videos)} YouTube tutorials!")
            
        except Exception as e:
            log_error("save custom recipe", e)
            print("Error saving custom recipe.")

    def _get_available_ingredients(self) -> List[str]:
        """Get list of available ingredients from inventory manager."""
        try:
            # Try to get ingredients from inventory manager
            if hasattr(self.inventory_mgr, 'get_current_inventory'):
                inventory = self.inventory_mgr.get_current_inventory()
                ingredients = []
                
                # Debug: Show what we got from inventory
                import logging
                logging.debug(f"Raw inventory from manager: {inventory}")
                
                if inventory:
                    for item in inventory:
                        if isinstance(item, dict):
                            name = item.get('name', '').lower()
                            if name:  # Only add non-empty names
                                ingredients.append(name)
                        else:
                            name = str(item).lower()
                            if name:
                                ingredients.append(name)
                    
                    logging.debug(f"Extracted ingredients: {ingredients}")
                    return ingredients
            
            # Return empty list if no inventory
            return []
            
        except Exception as e:
            log_error("get available ingredients", e)
            # Return empty list on error instead of fake ingredients
            return []

    def _generate_context_aware_recipe(self, ingredients: List[str], context: Dict) -> Optional[Dict]:
        """Generate a single context-aware recipe."""
        # Choose recipe category based on available ingredients
        recipe_type = self._determine_recipe_type(ingredients)
        
        if not recipe_type:
            return None
        
        # Select template and components
        templates = self.recipe_templates.get(recipe_type, [])
        if not templates:
            return None
            
        template = random.choice(templates)
        
        # Build recipe components
        components = self._build_recipe_components(ingredients, context)
        
        try:
            recipe_name = template.format(**components)
            
            # Get cuisine with intelligent detection
            cuisine = self._determine_cuisine_intelligent(components, context)
            
            # Create proper recipe hash for uniqueness checking (Fix for Issue 1)
            selected_ingredients = self._select_recipe_ingredients(ingredients, components)
            recipe_hash = generate_recipe_hash(
                recipe_name=recipe_name,
                ingredients=selected_ingredients,
                cooking_method=components.get('cooking_method', ''),
                cuisine=cuisine,
                dietary_tags=context.get('dietary_restrictions', {}).get('diet_types', [])
            )
            
            # Use household_size for servings
            household_size = context.get('household_size', 2)
            servings = max(household_size, 2)  # Minimum 2 servings
            
            recipe = {
                'name': recipe_name,
                'type': recipe_type,
                'ingredients': selected_ingredients,
                'hash': recipe_hash,
                'description': self._generate_description(recipe_type, components),
                'cooking_time': self._estimate_cooking_time(recipe_type),
                'difficulty': self._determine_difficulty(recipe_type, len(components)),
                'servings': str(servings),  # Context-aware servings
                'cuisine': cuisine
            }
            
            return recipe
            
        except KeyError as e:
            # Template formatting failed, try again
            return None
    
    def _generate_unique_recipe(self, ingredients: List[str]) -> Optional[Dict]:
        """Legacy method - generates recipe without context. Use _generate_context_aware_recipe instead."""
        context = self.context_mgr.get_unified_context() if self.context_mgr else {}
        return self._generate_context_aware_recipe(ingredients, context)

    def _determine_recipe_type(self, ingredients: List[str]) -> str:
        """Determine what type of recipe to make based on ingredients."""
        # Priority-based selection for better variety
        if 'paneer' in ingredients:
            return random.choice(['curry', 'stir_fry', 'salad'])
        elif 'milk' in ingredients or 'cream' in ingredients:
            return random.choice(['soup', 'curry', 'sauce'])
        elif 'noodles' in ingredients and 'pasta' in ingredients:
            return random.choice(['noodles', 'stir_fry'])
        elif 'noodles' in ingredients:
            return random.choice(['noodles', 'stir_fry', 'soup'])
        elif 'rice' in ingredients:
            return random.choice(['rice', 'stir_fry', 'curry'])
        elif 'curry' in ingredients or 'coconut' in ingredients:
            return random.choice(['curry', 'soup'])
        elif len([i for i in ingredients if i in ['onion', 'garlic', 'ginger']]) >= 2:
            return random.choice(['stir_fry', 'soup', 'curry'])
        else:
            return random.choice(['stir_fry', 'soup', 'salad', 'curry'])

    def _build_recipe_components(self, ingredients: List[str], context: Dict = None) -> Dict:
        """Build recipe components from available ingredients with context awareness."""
        if context is None:
            context = {
                'preferred_proteins': [],
                'dietary_restrictions': {'diet_types': []}
            }
        
        components = {}
        
        # Select vegetables with variations
        vegetables = [ing for ing in ingredients if ing in ['onion', 'garlic', 'ginger', 'scallion', 'chili', 'herbs', 'celery', 'carrot', 'bell_pepper', 'tomato', 'spinach', 'potato']]
        if vegetables:
            varied_vegetables = []
            selected_vegetables = random.sample(vegetables, min(3, len(vegetables)))
            
            for veg in selected_vegetables:
                if hasattr(self, 'ingredient_variations') and veg in self.ingredient_variations:
                    varied_vegetables.append(random.choice(self.ingredient_variations[veg]))
                else:
                    varied_vegetables.append(veg)
            
            if len(varied_vegetables) == 1:
                components['vegetables'] = varied_vegetables[0]
            elif len(varied_vegetables) == 2:
                components['vegetables'] = f"{varied_vegetables[0]} and {varied_vegetables[1]}"
            else:
                components['vegetables'] = f"{', '.join(varied_vegetables[:-1])}, and {varied_vegetables[-1]}"
        else:
            components['vegetables'] = 'mixed vegetables'
        
        # Select protein
        available_proteins = [ing for ing in ingredients if ing in ['chicken', 'beef', 'pork', 'tofu', 'egg', 'shrimp', 'fish', 'paneer', 'lentils', 'chickpeas']]
        
        diet_types = context.get('dietary_restrictions', {}).get('diet_types', [])
        if 'Vegetarian' in diet_types or 'Vegan' in diet_types:
            available_proteins = [p for p in available_proteins if p in ['tofu', 'paneer', 'lentils', 'chickpeas', 'mushrooms']]
        
        if available_proteins:
            preferred_proteins = context.get('preferred_proteins', [])
            preferred_available = [p for p in available_proteins if p.lower() in [pp.lower() for pp in preferred_proteins]]
            if preferred_available:
                components['protein'] = random.choice(preferred_available)
            else:
                components['protein'] = random.choice(available_proteins)
        else:
            if 'Vegetarian' in diet_types or 'Vegan' in diet_types:
                components['protein'] = random.choice(['tofu', 'mushrooms', 'paneer'])
            else:
                components['protein'] = random.choice(['tofu', 'egg', 'paneer'])
        
        # Select flavor profile based on ingredients
        if 'soy_sauce' in ingredients and 'ginger' in ingredients:
            components['flavor'] = random.choice(['asian-style', 'ginger-soy', 'umami', 'savory'])
        elif 'chili' in ingredients:
            components['flavor'] = random.choice(['spicy', 'hot', 'fiery', 'zesty'])
        elif 'herbs' in ingredients:
            components['flavor'] = random.choice(['herb-crusted', 'aromatic', 'fragrant', 'herbed'])
        else:
            components['flavor'] = random.choice(self.flavor_profiles)
        
        # Select cooking method
        components['cooking_method'] = random.choice(self.cooking_methods)
        components['dish_type'] = random.choice(self.dish_types)
        
        # Select sauce
        if 'soy_sauce' in ingredients:
            components['sauce'] = random.choice(['soy', 'teriyaki', 'ginger-soy'])
        else:
            components['sauce'] = random.choice(self.sauces)
        
        return components

    def _select_recipe_ingredients(self, available: List[str], components: Dict) -> List[str]:
        """Select specific ingredients for the recipe."""
        selected = []
        
        # Always include base ingredients if available
        base_ingredients = ['oil', 'salt', 'pepper']
        for base in base_ingredients:
            if base in available:
                selected.append(base)
        
        # Add main ingredients based on components
        vegetables_text = components.get('vegetables', '').lower()
        for veg in ['garlic', 'ginger', 'onion', 'chili']:
            if veg in vegetables_text and veg in available:
                selected.append(veg)
        
        # Add protein if specified and available
        protein = components.get('protein', '')
        if protein in available:
            selected.append(protein)
        
        # Add sauce/seasoning
        sauce_ingredients = ['soy_sauce', 'oyster_sauce', 'sesame_oil', 'rice_vinegar']
        for sauce in sauce_ingredients:
            if sauce in available:
                selected.append(sauce)
                break
        
        # Add main carbohydrate
        carbs = ['noodles', 'rice', 'pasta']
        for carb in carbs:
            if carb in available:
                selected.append(carb)
                break
        
        return list(set(selected))  # Remove duplicates

    def _generate_description(self, recipe_type: str, components: Dict) -> str:
        """Generate a brief description for the recipe."""
        descriptions = {
            'noodles': f"Delicious {components.get('cooking_method', 'stir-fried')} noodles with aromatic {components.get('vegetables', 'vegetables')} and savory flavors",
            'rice': f"Flavorful {components.get('flavor', 'savory')} rice dish featuring {components.get('vegetables', 'fresh vegetables')} and {components.get('protein', 'protein')}",
            'stir_fry': f"Quick and healthy {components.get('cooking_method', 'stir-fried')} dish with {components.get('vegetables', 'crisp vegetables')} and {components.get('protein', 'protein')}",
            'soup': f"Warming and comforting {components.get('flavor', 'savory')} soup with fresh {components.get('vegetables', 'ingredients')} and tender {components.get('protein', 'protein')}",
            'curry': f"Rich and flavorful {components.get('flavor', 'aromatic')} curry with {components.get('vegetables', 'vegetables')} and {components.get('protein', 'protein')}",
            'salad': f"Fresh and nutritious {components.get('flavor', 'crisp')} salad with vibrant {components.get('vegetables', 'vegetables')} and {components.get('protein', 'protein')}"
        }
        return descriptions.get(recipe_type, f"Delicious {components.get('flavor', 'homemade')} dish with fresh ingredients")

    def _estimate_cooking_time(self, recipe_type: str) -> str:
        """Estimate cooking time based on recipe type."""
        time_estimates = {
            'noodles': random.choice(['10-15', '15-20', '12-18']),
            'rice': random.choice(['20-25', '25-30', '18-25']),
            'stir_fry': random.choice(['8-12', '10-15', '12-18']),
            'soup': random.choice(['25-35', '30-40', '20-30']),
            'curry': random.choice(['30-45', '35-50', '25-40']),
            'salad': random.choice(['5-10', '8-12', '10-15'])
        }
        return time_estimates.get(recipe_type, '15-25')

    def _determine_difficulty(self, recipe_type: str, num_components: int) -> str:
        """Determine recipe difficulty."""
        if recipe_type in ['salad'] or num_components <= 3:
            return 'Easy'
        elif recipe_type in ['stir_fry', 'noodles'] or num_components <= 5:
            return random.choice(['Easy', 'Medium'])
        elif recipe_type in ['curry', 'soup']:
            return random.choice(['Medium', 'Medium-Hard'])
        else:
            return 'Medium'

    def _determine_cuisine(self, components: Dict) -> str:
        """Legacy method - uses determine_cuisine_intelligent for detection."""
        return determine_cuisine_intelligent(
            components, 
            {}, 
            default_cuisine=get_default_cuisine_from_config()
        )
    
    def _determine_cuisine_intelligent(self, components: Dict, context: Dict) -> str:
        """Determine cuisine based on user preferences, ingredients, and location."""
        return determine_cuisine_intelligent(
            components,
            context,
            default_cuisine=get_default_cuisine_from_config()
        )
    
    def _determine_cuisine_from_preferences(self, components: Dict, context: Dict) -> str:
        """Legacy method - redirects to _determine_cuisine_intelligent."""
        return self._determine_cuisine_intelligent(components, context)
    
    def _filter_by_dietary_restrictions(self, ingredients: List[str], restrictions: Dict) -> List[str]:
        """Remove ingredients that violate dietary restrictions."""
        filtered = ingredients.copy()
        
        # Remove allergens
        for allergen in restrictions.get('allergies', []):
            allergen_keywords = self._get_allergen_keywords(allergen)
            filtered = [ing for ing in filtered 
                       if not any(keyword in ing.lower() for keyword in allergen_keywords)]
        
        # Apply diet type filters (vegetarian, vegan, etc.)
        diet_types = restrictions.get('diet_types', [])
        if 'Vegetarian' in diet_types or 'Vegan' in diet_types:
            meat_keywords = ['chicken', 'beef', 'pork', 'fish', 'shrimp', 'meat', 'lamb', 'turkey', 'duck']
            filtered = [ing for ing in filtered if not any(m in ing.lower() for m in meat_keywords)]
        
        if 'Vegan' in diet_types:
            dairy_keywords = ['milk', 'cheese', 'butter', 'cream', 'yogurt', 'egg', 'eggs']
            filtered = [ing for ing in filtered if not any(d in ing.lower() for d in dairy_keywords)]
        
        # Apply cultural restrictions (e.g., no pork, no beef)
        cultural_restrictions = restrictions.get('cultural_restrictions', [])
        if 'No Pork' in cultural_restrictions or 'Halal' in cultural_restrictions:
            filtered = [ing for ing in filtered if 'pork' not in ing.lower()]
        
        if 'No Beef' in cultural_restrictions or 'Vegetarian' in cultural_restrictions:
            filtered = [ing for ing in filtered if 'beef' not in ing.lower()]
        
        return filtered
    
    def _get_allergen_keywords(self, allergen: str) -> List[str]:
        """Map allergen to ingredient keywords."""
        allergen_map = {
            'Dairy': ['milk', 'cheese', 'butter', 'cream', 'yogurt', 'cottage cheese', 'sour cream'],
            'Nuts': ['almond', 'peanut', 'cashew', 'walnut', 'hazelnut', 'pecan', 'pistachio', 'macadamia'],
            'Shellfish': ['shrimp', 'crab', 'lobster', 'prawn', 'scallop', 'oyster', 'mussel'],
            'Gluten': ['wheat', 'flour', 'bread', 'pasta', 'noodles', 'barley', 'rye'],
            'Eggs': ['egg', 'eggs', 'mayonnaise', 'custard'],
            'Soy': ['soy', 'tofu', 'soybean', 'miso', 'tempeh'],
            'Fish': ['fish', 'tuna', 'salmon', 'sardine', 'anchovy'],
            'Sesame': ['sesame', 'tahini']
        }
        return allergen_map.get(allergen, [allergen.lower()])

    def _is_recipe_unique(self, new_recipe: Dict, existing_recipes: List[Dict], threshold: float = 0.75) -> bool:
        """Check if recipe is unique compared to existing ones using modular checker."""
        return check_uniqueness_with_threshold(new_recipe, existing_recipes, threshold)

    def _calculate_similarity(self, name1: str, name2: str) -> float:
        """Calculate similarity between two recipe names using word overlap."""
        if not name1 or not name2:
            return 0.0
            
        words1 = set(name1.split())
        words2 = set(name2.split())
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        return len(intersection) / len(union) if union else 0.0

    def view_favorite_recipes(self):
        """Display user's favorite recipes from database with YouTube links."""
        try:
            # Get current user ID - use email as primary identifier (same logic as save)
            user_id = None
            
            # Try multiple sources for user ID
            if self.user_mgr.current_user:
                # First try email (most reliable for OAuth)
                user_id = self.user_mgr.current_user.get('email')
                # Fallback to username if email not available
                if not user_id:
                    user_id = self.user_mgr.current_user.get('username')
            
            # Fallback to current_user_id attribute
            if not user_id:
                user_id = getattr(self.user_mgr, 'current_user_id', None)
            
            # Final fallback
            if not user_id:
                user_id = 'default_user'
                logger.warning("Could not determine user ID for viewing recipes")
            
            # Debug: Log the user ID being used
            logger.info(f"Viewing recipes for user_id: {user_id}")
            
            # Use DatabaseConnectionContext (same as save method) for consistency
            try:
                with DatabaseConnectionContext(self.database.get_client()) as db:
                    favorites = list(db['favorite_recipes'].find({'user_id': user_id}))
                    logger.info(f"Found {len(favorites)} favorite recipe(s) for user {user_id}")
            except Exception as db_error:
                logger.error(f"Database error while fetching recipes: {db_error}")
                favorites = []
            
            if not favorites:
                print("You haven't saved any favorite recipes yet!")
                print("Try generating some recipes and save your favorites.")
                return
            
            print(f"\n Your Favorite Recipes ({len(favorites)} saved):")
            print("=" * 40)
            
            for i, recipe in enumerate(favorites, 1):
                print(f"{i}. {recipe['name']}")
                print(f"{recipe.get('description', 'No description')}")
                print(f"{recipe.get('cooking_time', 'Unknown')} minutes")
                print(f"{recipe.get('difficulty', 'Unknown')} difficulty")
                
                # Display YouTube video count if available
                youtube_videos = recipe.get('youtube_videos', [])
                if youtube_videos:
                    print(f"{len(youtube_videos)} YouTube tutorial(s) available")
                print()
            
            # Option to view detailed recipe
            choice = input("Enter recipe number for details (or press Enter to continue): ")
            if choice.isdigit():
                recipe_num = int(choice) - 1
                if 0 <= recipe_num < len(favorites):
                    # Ensure recipe has YouTube videos if available
                    selected_recipe = favorites[recipe_num]
                    if not selected_recipe.get('youtube_videos') and self.youtube_service:
                        # Fetch videos if not stored
                        videos = self._fetch_youtube_videos(selected_recipe['name'])
                        if videos:
                            selected_recipe['youtube_videos'] = videos
                    self._display_detailed_recipe(selected_recipe)
                    
        except Exception as e:
            log_error("view favorite recipes", e)
            print("Error loading favorite recipes.")

    def clear_session_recipes(self):
        """Clear current session recipes (useful for fresh start)."""
        self.session_recipes.clear()
        self.generated_recipes.clear()
        print("Recipe session cleared!")

# ===== From src/recipes/recipe_generation.py =====
"""
Recipe Generation with Progressive Relaxation
Prevents infinite loops and ensures sufficient recipe diversity (Fix for Issue 2)
"""

import logging
from typing import List, Dict, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class RecipeGenerationResult:
    """Container for recipe generation results with metadata."""
    
    def __init__(self, recipes: List[Dict], requested: int, generated: int, 
                 reason: str = "success", warnings: List[str] = None):
        self.recipes = recipes
        self.requested = requested
        self.generated = generated
        self.reason = reason
        self.warnings = warnings or []
    
    def to_dict(self) -> Dict:
        return {
            "recipes": self.recipes,
            "requested": self.requested,
            "generated": self.generated,
            "reason": self.reason,
            "warnings": self.warnings
        }


def generate_recipes_with_relaxation(
    num_recipes: int,
    generator_func: Callable,
    uniqueness_checker: Callable,
    ingredients: List[str],
    context: Dict
) -> RecipeGenerationResult:
    """
    Generate recipes with progressive relaxation of uniqueness threshold.
    
    Strategy:
    1. Attempts 0-50%: Use 75% similarity threshold (strict)
    2. Attempts 50-75%: Reduce to 60% similarity threshold (moderate)
    3. Attempts 75-100%: Reduce to 50% similarity threshold (relaxed)
    4. If still insufficient: Try different cuisines
    
    Args:
        num_recipes: Number of recipes requested
        generator_func: Function to generate a single recipe
        uniqueness_checker: Function to check if recipe is unique
        ingredients: Available ingredients
        context: User context dict
    
    Returns:
        RecipeGenerationResult with recipes and metadata
    """
    recipes = []
    max_attempts = num_recipes * 5
    attempts = 0
    warnings = []
    
    # Similarity thresholds for progressive relaxation
    threshold_strict = 0.75
    threshold_moderate = 0.60
    threshold_relaxed = 0.50
    
    current_threshold = threshold_strict
    cuisine_rotation = None
    
    logger.info(f"Starting recipe generation: requested={num_recipes}, max_attempts={max_attempts}")
    
    while len(recipes) < num_recipes and attempts < max_attempts:
        # Progressive relaxation logic
        progress = attempts / max_attempts
        
        if progress > 0.75 and current_threshold != threshold_relaxed:
            current_threshold = threshold_relaxed
            logger.warning(f"Relaxed uniqueness threshold to {current_threshold} (attempt {attempts})")
            warnings.append(f"Relaxed uniqueness criteria to generate more recipes")
        elif progress > 0.50 and current_threshold == threshold_strict:
            current_threshold = threshold_moderate
            logger.info(f"Reduced uniqueness threshold to {current_threshold} (attempt {attempts})")
        
        # After 75% of attempts, try different cuisines
        if progress > 0.75 and cuisine_rotation is None:
            cuisine_rotation = ["Italian", "Mexican", "Asian", "Mediterranean", "American"]
            logger.info("Activating cuisine rotation for diversity")
            warnings.append("Exploring different cuisines for variety")
        
        # Modify context for cuisine rotation
        generation_context = context.copy()
        if cuisine_rotation:
            generation_context['force_cuisine'] = cuisine_rotation[attempts % len(cuisine_rotation)]
        
        # Generate recipe
        recipe = generator_func(ingredients, generation_context)
        
        if recipe:
            # Check uniqueness with current threshold
            is_unique = uniqueness_checker(recipe, recipes, threshold=current_threshold)
            
            if is_unique:
                recipes.append(recipe)
                logger.info(f"Generated recipe {len(recipes)}/{num_recipes}: {recipe.get('name')}")
        
        attempts += 1
    
    # Determine result reason
    if len(recipes) == num_recipes:
        reason = "success"
    elif len(recipes) > 0:
        reason = "partial_success"
        warning_msg = f"Generated {len(recipes)}/{num_recipes} recipes due to limited ingredient diversity"
        warnings.append(warning_msg)
        logger.warning(warning_msg)
    else:
        reason = "failed"
        warnings.append("Unable to generate any recipes with available ingredients")
        logger.error("Recipe generation failed completely")
    
    return RecipeGenerationResult(
        recipes=recipes,
        requested=num_recipes,
        generated=len(recipes),
        reason=reason,
        warnings=warnings
    )


def check_uniqueness_with_threshold(new_recipe: Dict, existing_recipes: List[Dict], 
                                    threshold: float = 0.75) -> bool:
    """
    Check if recipe is unique with configurable similarity threshold.
    
    Args:
        new_recipe: Recipe to check
        existing_recipes: List of existing recipes
        threshold: Similarity threshold (0.0-1.0). Higher = stricter uniqueness check
    
    Returns:
        True if recipe is unique enough, False otherwise
    """
    if not existing_recipes:
        return True
    
    new_hash = new_recipe.get('hash')
    new_name = new_recipe.get('name', '').lower()
    
    for existing in existing_recipes:
        # Exact hash match = duplicate
        if existing.get('hash') == new_hash:
            return False
        
        # Check name similarity
        existing_name = existing.get('name', '').lower()
        similarity = _calculate_word_similarity(new_name, existing_name)
        
        if similarity > threshold:
            return False
    
    return True


def _calculate_word_similarity(name1: str, name2: str) -> float:
    """Calculate Jaccard similarity between two recipe names."""
    if not name1 or not name2:
        return 0.0
    
    words1 = set(name1.split())
    words2 = set(name2.split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    
    return len(intersection) / len(union) if union else 0.0


# ===== From src/recipes/recipe_parser.py =====
"""
Robust Recipe Parser with Regex-Based Section Detection
Fixes fragile section detection with fuzzy matching (Fix for Issue 2)
"""

import re
import logging
from typing import List, Dict, Tuple, Set
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


# Section aliases for fuzzy matching
SECTION_ALIASES = {
    'ingredients': ['ingredient', 'items', 'what you need', 'you will need', 'required'],
    'ingredients_selected': ['ingredients selected', 'selected ingredients', 'using', 'chosen ingredients'],
    'ingredients_not_used': ['ingredients not used', 'not using', 'unused ingredients', 'excluded ingredients'],
    'instructions': ['instruction', 'steps', 'directions', 'method', 'how to make', 'procedure'],
    'time': ['time', 'timing', 'duration'],
    'serving': ['serving', 'servings', 'serves', 'yield'],
    'cuisine': ['cuisine', 'style', 'type'],
    'dietary_tags': ['dietary', 'diet', 'tags', 'restrictions', 'suitable for'],
    'difficulty': ['difficulty', 'level', 'skill']
}


class RecipeValidationResult:
    """Container for recipe validation results."""
    
    def __init__(self, is_valid: bool, missing_sections: List[str], 
                 malformed_sections: List[str], warnings: List[str]):
        self.is_valid = is_valid
        self.missing_sections = missing_sections
        self.malformed_sections = malformed_sections
        self.warnings = warnings
    
    def to_dict(self) -> Dict:
        return {
            "is_valid": self.is_valid,
            "missing_sections": self.missing_sections,
            "malformed_sections": self.malformed_sections,
            "warnings": self.warnings
        }


def parse_recipes_robust(recipes_text: str) -> Tuple[List[Dict], RecipeValidationResult]:
    """
    Parse recipes with robust section detection using regex.
    
    Features:
    - Regex-based header detection (handles extra spaces/punctuation)
    - Fuzzy section name matching
    - Validation of required sections
    - Detailed error reporting
    
    Args:
        recipes_text: Raw recipe text from AI
    
    Returns:
        Tuple of (parsed_recipes, validation_result)
    """
    recipes = []
    current_recipe = None
    current_section = None
    missing_sections_all = []
    malformed_sections_all = []
    warnings = []
    
    lines = recipes_text.strip().split('\n')
    
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        
        # Skip empty lines
        if not line:
            continue
        
        # Recipe title detection (### Recipe: Title)
        title_match = re.match(r'^#{2,3}\s*Recipe\s*:\s*(.+)$', line, re.IGNORECASE)
        if title_match:
            # Save previous recipe
            if current_recipe and current_recipe.get('name'):
                validation = _validate_recipe(current_recipe)
                if not validation.is_valid:
                    missing_sections_all.extend(validation.missing_sections)
                    malformed_sections_all.extend(validation.malformed_sections)
                    warnings.extend(validation.warnings)
                recipes.append(current_recipe)
            
            # Start new recipe
            title = title_match.group(1).strip()
            current_recipe = {
                'name': title,
                'title': title,
                'description': '',
                'cuisine': '',
                'dietary_tags': [],
                'ingredients': [],
                'ingredients_selected': [],  # New: Track AI's ingredient selection
                'ingredients_not_used': [],  # New: Track unused ingredients
                'instructions': [],
                'prep_time': '',
                'cook_time': '',
                'total_time': '',
                'servings': '',
                'difficulty': ''
            }
            current_section = None
            continue
        
        # Section header detection (#### Section Name:)
        # Robust regex: handles extra spaces, optional colon, case-insensitive
        section_match = re.match(r'^#{2,4}\s*(.+?)\s*:?\s*$', line, re.IGNORECASE)
        if section_match:
            section_raw = section_match.group(1).strip()
            # Normalize and match to known sections
            current_section = _normalize_section_name(section_raw)
            if not current_section:
                warnings.append(f"Line {line_num}: Unknown section '{section_raw}'")
            continue
        
        # Content parsing
        if current_recipe and current_section:
            _parse_section_content(current_recipe, current_section, line)
        elif current_recipe and not current_section and not current_recipe['description']:
            # First line after title is description
            current_recipe['description'] = line
    
    # Save last recipe
    if current_recipe and current_recipe.get('name'):
        validation = _validate_recipe(current_recipe)
        if not validation.is_valid:
            missing_sections_all.extend(validation.missing_sections)
            malformed_sections_all.extend(validation.malformed_sections)
            warnings.extend(validation.warnings)
        recipes.append(current_recipe)
    
    # Overall validation
    is_valid = len(recipes) > 0 and not missing_sections_all
    validation_result = RecipeValidationResult(
        is_valid=is_valid,
        missing_sections=list(set(missing_sections_all)),
        malformed_sections=list(set(malformed_sections_all)),
        warnings=warnings
    )
    
    return recipes, validation_result


def _normalize_section_name(section_raw: str) -> str:
    """
    Normalize section name with fuzzy matching.
    
    Args:
        section_raw: Raw section name from text
    
    Returns:
        Normalized section name or None if no match
    """
    # Clean up section name
    section_clean = section_raw.lower().strip()
    section_clean = re.sub(r'[^a-z\s]', '', section_clean)
    section_clean = re.sub(r'\s+', ' ', section_clean)
    
    # Exact match first
    for canonical, aliases in SECTION_ALIASES.items():
        if section_clean in aliases or section_clean == canonical:
            return canonical
    
    # Fuzzy match (similarity > 0.8)
    best_match = None
    best_score = 0.8
    
    for canonical, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            score = SequenceMatcher(None, section_clean, alias).ratio()
            if score > best_score:
                best_score = score
                best_match = canonical
    
    return best_match


def _parse_section_content(recipe: Dict, section: str, line: str) -> None:
    """
    Parse content for a specific section.
    
    Args:
        recipe: Recipe dict to update
        section: Normalized section name
        line: Content line
    """
    if section == 'ingredients':
        # Match list items: "- Item" or "* Item" or "- Item"
        item_match = re.match(r'^[-*-]\s*(.+)$', line)
        if item_match:
            recipe['ingredients'].append(item_match.group(1).strip())
    
    elif section == 'ingredients_selected':
        # Parse ingredients selected by AI (comma-separated or list)
        if ',' in line:
            items = [item.strip() for item in line.split(',') if item.strip()]
            recipe['ingredients_selected'].extend(items)
        else:
            item_match = re.match(r'^[-*-]\s*(.+)$', line)
            if item_match:
                recipe['ingredients_selected'].append(item_match.group(1).strip())
            elif line and not line.startswith('#'):
                recipe['ingredients_selected'].append(line)
    
    elif section == 'ingredients_not_used':
        # Parse ingredients not used by AI (comma-separated or list)
        if ',' in line:
            items = [item.strip() for item in line.split(',') if item.strip()]
            recipe['ingredients_not_used'].extend(items)
        else:
            item_match = re.match(r'^[-*-]\s*(.+)$', line)
            if item_match:
                recipe['ingredients_not_used'].append(item_match.group(1).strip())
            elif line and not line.startswith('#'):
                recipe['ingredients_not_used'].append(line)
    
    elif section == 'instructions':
        # Match numbered steps: "1. Step" or "1) Step"
        step_match = re.match(r'^(\d+)[.)]\s*(.+)$', line)
        if step_match:
            recipe['instructions'].append(step_match.group(2).strip())
    
    elif section == 'time':
        # Parse time fields
        if 'prep' in line.lower():
            time_match = re.search(r':\s*(.+)$', line)
            if time_match:
                recipe['prep_time'] = time_match.group(1).strip()
        elif 'cook' in line.lower():
            time_match = re.search(r':\s*(.+)$', line)
            if time_match:
                recipe['cook_time'] = time_match.group(1).strip()
        elif 'total' in line.lower():
            time_match = re.search(r':\s*(.+)$', line)
            if time_match:
                recipe['total_time'] = time_match.group(1).strip()
    
    elif section == 'serving':
        # Parse serving fields
        if 'servings' in line.lower() or 'serves' in line.lower():
            serving_match = re.search(r':\s*(.+)$', line)
            if serving_match:
                recipe['servings'] = serving_match.group(1).strip()
        elif 'difficulty' in line.lower():
            diff_match = re.search(r':\s*(.+)$', line)
            if diff_match:
                recipe['difficulty'] = diff_match.group(1).strip()
    
    elif section == 'cuisine':
        # Extract cuisine value
        cuisine_match = re.search(r':\s*(.+)$', line)
        if cuisine_match:
            recipe['cuisine'] = cuisine_match.group(1).strip()
        elif not recipe['cuisine']:
            recipe['cuisine'] = line
    
    elif section == 'dietary_tags':
        # Parse dietary tags (comma-separated or list)
        if ',' in line:
            tags = [tag.strip() for tag in line.split(',')]
            recipe['dietary_tags'].extend(tags)
        else:
            tag_match = re.match(r'^[-*-]\s*(.+)$', line)
            if tag_match:
                recipe['dietary_tags'].append(tag_match.group(1).strip())
            elif line:
                recipe['dietary_tags'].append(line)
    
    elif section == 'difficulty':
        # Extract difficulty
        diff_match = re.search(r':\s*(.+)$', line)
        if diff_match:
            recipe['difficulty'] = diff_match.group(1).strip()
        elif not recipe['difficulty']:
            recipe['difficulty'] = line


def _validate_recipe(recipe: Dict) -> RecipeValidationResult:
    """
    Validate that recipe has minimum required sections.
    
    Required sections:
    - name
    - ingredients (at least 1)
    - instructions (at least 1)
    
    Args:
        recipe: Recipe dict to validate
    
    Returns:
        RecipeValidationResult
    """
    missing_sections = []
    malformed_sections = []
    warnings = []
    
    # Check required fields
    if not recipe.get('name'):
        missing_sections.append('name')
    
    if not recipe.get('ingredients'):
        missing_sections.append('ingredients')
    elif len(recipe['ingredients']) < 1:
        malformed_sections.append('ingredients (empty)')
    
    if not recipe.get('instructions'):
        missing_sections.append('instructions')
    elif len(recipe['instructions']) < 1:
        malformed_sections.append('instructions (empty)')
    
    # Check optional but recommended fields
    if not recipe.get('description'):
        warnings.append(f"Recipe '{recipe.get('name')}' missing description")
    
    if not recipe.get('servings'):
        warnings.append(f"Recipe '{recipe.get('name')}' missing servings")
    
    is_valid = len(missing_sections) == 0 and len(malformed_sections) == 0
    
    return RecipeValidationResult(
        is_valid=is_valid,
        missing_sections=missing_sections,
        malformed_sections=malformed_sections,
        warnings=warnings
    )
