import os
import re
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Optional
from PIL import Image
import google.generativeai as genai
from src.config.constants import GEMINI_API_KEY, GEMINI_VISION_MODEL, GEMINI_TEXT_MODEL
from src.config.constants import (
    INVENTORY_PROMPT_TEMPLATE, RECIPE_PROMPT_TEMPLATE,
    INGREDIENT_SYNONYMS, COMMON_ALLERGENS
)
from src.utils.helpers import log_error, is_cache_valid

class VisionService:
    """Handles all AI vision-related operations."""
    def __init__(self, cache_dir="./cache"):
        self.cache_dir = cache_dir
        self.ai_cache = {}
        self.vision_model = None
        self.text_model = None
        self._setup_gemini()

    def _setup_gemini(self) -> None:
        """Configure Gemini AI with API key."""
        try:
            genai.configure(api_key=GEMINI_API_KEY)
            # Use Pro model for vision as requested ("2.5 pro")
            self.vision_model = genai.GenerativeModel(GEMINI_VISION_MODEL)
            # Use Flash model for text as requested ("gemini-3-flash")
            self.text_model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            print(f"\nGemini AI configured successfully! (Vision: {GEMINI_VISION_MODEL}, Text: {GEMINI_TEXT_MODEL})")
        except Exception as e:
            log_error("Gemini AI setup", e)
            self.vision_model = None
            self.text_model = None

    def _extract_text_from_response(self, response) -> str:
        """Safely extract text from Gemini response handling multiple parts and potential blockages."""
        try:
            # Try the standard accessor first
            try:
                return response.text
            except (ValueError, AttributeError):
                pass
            
            # Fallback for complex or multi-part responses
            full_text = []
            if hasattr(response, 'candidates') and response.candidates:
                for candidate in response.candidates:
                    if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                        for part in candidate.content.parts:
                            if hasattr(part, 'text'):
                                full_text.append(part.text)
            
            if full_text:
                return "".join(full_text)
                
            # Last resort: check if there are parts directly on response (some versions)
            if hasattr(response, 'parts'):
                return "".join([part.text for part in response.parts if hasattr(part, 'text')])
                
            return str(response)
        except Exception as e:
            log_error("extracting text from AI response", e)
            return ""

    def get_cached_response(self, image_path: str, mode: str) -> Optional[str]:
        try:
            with open(image_path, 'rb') as f:
                image_hash = hashlib.md5(f.read()).hexdigest()
               
            cache_key = f"{image_hash}_{mode}"
           
            # Check memory cache first
            if cache_key in self.ai_cache:
                print("\nUsing cached AI response...")
                return self.ai_cache[cache_key]
               
            # Check disk cache
            cache_file = os.path.join(self.cache_dir, f"{cache_key}.json")
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    if is_cache_valid(data['timestamp']):
                        print("\nUsing disk-cached AI response...")
                        self.ai_cache[cache_key] = data['response']
                        return data['response']
                    else:
                        os.remove(cache_file)  # Remove stale cache
           
            return None
        except Exception as e:
            log_error("cache retrieval", e)
            return None

    def cache_response(self, image_path: str, mode: str, response: str) -> None:
        try:
            with open(image_path, 'rb') as f:
                image_hash = hashlib.md5(f.read()).hexdigest()
            cache_key = f"{image_hash}_{mode}"

            # Update memory cache
            self.ai_cache[cache_key] = response

            # Update disk cache
            cache_file = os.path.join(self.cache_dir, f"{cache_key}.json")
            if not os.path.exists(self.cache_dir):
                os.makedirs(self.cache_dir)
            with open(cache_file, 'w') as f:
                json.dump({
                    "timestamp": datetime.now().isoformat(),
                    "response": response,
                    "mode": mode
                }, f)
        except Exception as e:
            log_error("cache storage", e)

    def analyze_inventory(self, image_path: str) -> Optional[str]:
        """Analyze image to detect food inventory using the Pro model."""
        if not self.vision_model:
            print("\nGemini Vision AI not configured properly")
            return None
            
        cached_response = self.get_cached_response(image_path, 'items')
        if cached_response:
            return cached_response
            
        try:
            print(f"\nGenerating AI analysis using {GEMINI_VISION_MODEL}...")
            response = self.vision_model.generate_content([INVENTORY_PROMPT_TEMPLATE, Image.open(image_path)])
            response_text = self._extract_text_from_response(response)
           
            self.cache_response(image_path, 'items', response_text)
           
            return response_text
           
        except Exception as e:
            log_error("AI inventory analysis", e)
            return None

    def generate_recipes(self, inventory_text: str, cuisine: str = "International", 
                         num_recipes: int = 3, dietary_restrictions: str = "None",
                         allergies: list = None, cultural_restrictions: list = None,
                         age_groups: list = None, budget: str = "Medium",
                         cuisine_preferences: list = None, household_size: int = 2) -> Optional[str]:
        """Generate recipes using the Flash model with full user context."""
        if not self.text_model:
            print("\nGemini Text AI not configured properly")
            return None
            
        # Set defaults for optional parameters
        if allergies is None:
            allergies = []
        if cultural_restrictions is None:
            cultural_restrictions = []
        if age_groups is None:
            age_groups = []
        if cuisine_preferences is None:
            cuisine_preferences = [cuisine]
            
        try:
            # Format lists for prompt
            allergies_str = ", ".join(allergies) if allergies else "None"
            cultural_restrictions_str = ", ".join(cultural_restrictions) if cultural_restrictions else "None"
            age_groups_str = ", ".join(age_groups) if age_groups else "Adults"
            cuisine_preferences_str = ", ".join(cuisine_preferences) if cuisine_preferences else cuisine
            
            # Format the prompt with full context data
            prompt = RECIPE_PROMPT_TEMPLATE.format(
                inventory=inventory_text,
                cuisine=cuisine,
                num_recipes=num_recipes,
                dietary_restrictions=dietary_restrictions,
                allergies=allergies_str,
                cultural_restrictions=cultural_restrictions_str,
                household_size=household_size,
                age_groups=age_groups_str,
                cuisine_preferences=cuisine_preferences_str,
                budget=budget
            )
            
            print(f"\nGenerating AI recipe suggestions using {GEMINI_TEXT_MODEL}...")
            
            # Configure for complete 3-recipe generation
            generation_config = {
                'temperature': 0.7,  # Lower = more focused, faster
                'top_p': 0.9,
                'top_k': 40,
                'max_output_tokens': 3072,  # Increased to ensure 3 complete recipes
            }
            
            response = self.text_model.generate_content(
                prompt,
                generation_config=generation_config,
            )
            response_text = self._extract_text_from_response(response)
           
            return response_text
           
        except Exception as e:
            log_error("recipe generation", e)
            return None

    def check_allergy_risk(self, item_name: str, user_allergies: List[str]) -> bool:
        """Check if an item poses allergy risk for the user."""
        if not user_allergies:
            return False
           
        item_name = self.normalize_ingredient_name(item_name)
       
        for allergy in user_allergies:
            allergy = allergy.lower()
            if allergy in COMMON_ALLERGENS:
                for allergen in COMMON_ALLERGENS[allergy]:
                    if allergen in item_name:
                        return True
            elif allergy in item_name:
                return True
               
        return False

    def parse_inventory(self, items_text: str) -> List[Dict]:
        """Parse detected items text into structured format."""
        parsed_items = []
        current_category = None
        lines = items_text.split('\n')
       
        for line in lines:
            line = line.strip()
            if not line:
                continue
           
            if ':' in line and not line.startswith('-'):
                current_category = line.rstrip(':')
                continue
               
            if line.startswith('-') and current_category:
                item_info = {'category': current_category}
               
                item_text = line[1:].strip()
               
                if '(x' in item_text:
                    item_name, quantity_str = item_text.split('(x')
                    item_info['name'] = self.normalize_ingredient_name(item_name.strip())
                    item_info['quantity'] = quantity_str.rstrip(')')
                elif '(' in item_text and ')' in item_text:
                    parts = item_text.split('(')
                    item_info['name'] = self.normalize_ingredient_name(parts[0].strip())
                    item_info['notes'] = parts[1].rstrip(')')
                else:
                    item_info['name'] = self.normalize_ingredient_name(item_text)
               
                item_info['timestamp'] = datetime.now()
               
                parsed_items.append(item_info)
       
        return parsed_items

    def parse_recipes(self, recipes_text: str) -> List[Dict]:
        """Enhanced recipe parser with missing ingredients and nutrition extraction."""
        recipes = []
        
        # Split by explicit separator
        recipe_blocks = recipes_text.split('---END_RECIPE---')
        
        for block in recipe_blocks:
            block = block.strip()
            if not block or 'INCOMPLETE:' in block:
                continue
                
            current_recipe = None
            current_section = None
            
            for line in block.split('\n'):
                line = line.strip()
                
                # Skip empty lines
                if not line:
                    continue
                
                # Recipe title detection
                if line.startswith(('### Recipe:', '###Recipe:')):
                    if current_recipe and current_recipe.get('name'):
                        recipes.append(current_recipe)
                    title = line.split(':', 1)[1].strip() if ':' in line else line.split(' ', 2)[2].strip()
                    current_section = None
                    current_recipe = {
                        'name': title,
                        'title': title,
                        'description': '',
                        'cuisine': '',
                        'dietary_tags': [],
                        'ingredients': [],
                        'instructions': [],
                        'prep_time': '',
                        'cook_time': '',
                        'total_time': '',
                        'servings': '',
                        'difficulty': '',
                        'nutrition': {},
                        'missing_ingredients': []  # NEW: Shopping list
                    }
                
                # Section detection
                elif line.startswith('#### '):
                    section = line[5:].lower().replace(' ', '_').replace('(', '').replace(')', '').rstrip(':')
                    current_section = section
                
                # Content parsing
                elif current_recipe:
                    # Parse ingredients (with OK marker for fridge items)
                    if line.startswith('-') and current_section == 'ingredients':
                        current_recipe['ingredients'].append(line[1:].strip())
                    
                    # Parse instructions
                    elif line[0].isdigit() and line[1] in ('.', ')') and current_section == 'instructions':
                        step = line.split('.', 1)[1].strip() if '.' in line else line.split(')', 1)[1].strip()
                        current_recipe['instructions'].append(step)
                    
                    # Parse time information
                    elif current_section == 'time' and ':' in line:
                        if 'prep' in line.lower():
                            current_recipe['prep_time'] = line.split(':', 1)[1].strip()
                        elif 'cook' in line.lower():
                            current_recipe['cook_time'] = line.split(':', 1)[1].strip()
                        elif 'total' in line.lower():
                            current_recipe['total_time'] = line.split(':', 1)[1].strip()
                    
                    # Parse serving information
                    elif current_section == 'serving' and ':' in line:
                        if 'servings' in line.lower():
                            current_recipe['servings'] = line.split(':', 1)[1].strip()
                        elif 'difficulty' in line.lower():
                            current_recipe['difficulty'] = line.split(':', 1)[1].strip()
                    
                    # Parse nutrition information (NEW)
                    elif current_section == 'nutrition_per_serving' and line.startswith('-'):
                        if ':' in line:
                            nutrient, value = line[1:].split(':', 1)
                            current_recipe['nutrition'][nutrient.strip().lower()] = value.strip()
                    
                    # Parse missing ingredients / shopping list (NEW)
                    elif current_section == 'missing_ingredients_shopping_list' and line.startswith('-'):
                        ingredient_info = line[1:].strip()
                        current_recipe['missing_ingredients'].append(ingredient_info)
                    
                    # Parse cuisine
                    elif current_section == 'cuisine':
                        if current_recipe['cuisine'] == '':
                            current_recipe['cuisine'] = line
                    
                    # Parse dietary tags
                    elif current_section == 'dietary_tags':
                        if line.startswith('-'):
                            current_recipe['dietary_tags'].append(line[1:].strip())
                        else:
                            current_recipe['dietary_tags'].append(line)
                    
                    # Parse description (first non-section line after title)
                    elif not current_section and current_recipe['description'] == '':
                        current_recipe['description'] = line
            
            # Add the last recipe from this block
            if current_recipe and current_recipe.get('name'):
                recipes.append(current_recipe)
        
        # Filter out any empty recipes
        recipes = [r for r in recipes if r.get('name') and r.get('ingredients')]
        
        # Validation: warn if fewer recipes than expected
        if len(recipes) < 3:
            print(f"\nWARNING: Warning: AI generated only {len(recipes)} recipe(s) instead of 3.")
            print("Displaying what's available. Try again for more variety.")
        
        return recipes
       
    def normalize_ingredient_name(self, name: str) -> str:
        """Normalize ingredient names using regex patterns."""
        if not name:
            return ""
           
        name = name.lower().strip()
       
        # Remove quantity descriptors and units
        name = re.sub(r'\b\d+\s*(kg|g|ml|l|oz|lb|pound|ounce|liter|gram|kilo)\b', '', name)
        name = re.sub(r'\b(pieces?|chopped|halves|whole|sliced|diced|minced|grated)\b', '', name)
       
        # Remove parenthetical quantities and notes
        name = re.sub(r'\(.*?\)', '', name)
       
        # Remove special characters except hyphens between words
        name = re.sub(r'[^\w\s-]', '', name)
       
        # Clean up whitespace
        name = re.sub(r'\s+', ' ', name).strip()
       
        # Apply synonym mapping
        for original, synonym in INGREDIENT_SYNONYMS.items():
            if original in name:
                name = name.replace(original, synonym)
                break
       
        return name