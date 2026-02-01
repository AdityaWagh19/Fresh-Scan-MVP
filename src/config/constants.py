import os
from dotenv import load_dotenv

load_dotenv()

# ========== GLOBAL CONSTANTS ==========
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
CACHE_DIR = "./cache"
DEFAULT_SERVER_URL = "http://172.16.1.252:5000"
DEFAULT_CUISINE = "International"  # Default cuisine when no preference/detection available
GEMINI_VISION_MODEL = "gemini-2.5-pro"  # Higher accuracy for vision tasks
GEMINI_TEXT_MODEL = "gemini-2.5-flash"   # Fast performance for text tasks

# ========== STATIC CONFIGURATIONS ==========
DIET_OPTIONS = {
    '1': 'Vegan',
    '2': 'Vegetarian',
    '3': 'Non-Vegetarian',
    '4': 'Gluten-Free',
    '5': 'Keto/Low Carb'
}

ALLERGY_OPTIONS = {
    '1': 'Dairy',
    '2': 'Gluten',
    '3': 'Nuts',
    '4': 'Soy',
    '5': 'Seafood'
}

CUISINE_OPTIONS = {
    '1': 'Indian',
    '2': 'Continental',
    '3': 'Chinese',
    '4': 'Italian',
    '5': 'Mexican'
}

PROTEIN_OPTIONS = {
    '1': 'Lentils',
    '2': 'Paneer',
    '3': 'Tofu',
    '4': 'Eggs',
    '5': 'Meat'
}

AGE_GROUP_OPTIONS = {
    '1': 'Children',
    '2': 'Teens',
    '3': 'Adults',
    '4': 'Seniors'
}

CULTURAL_RESTRICTIONS = {
    '1': 'No Beef',
    '2': 'No Pork',
    '3': 'Halal',
    '4': 'Jain'
}

COOKING_FREQUENCY_OPTIONS = {
    '1': 'Daily',
    '2': 'Occasionally',
    '3': 'Rarely'
}

GROCERY_BUDGET_OPTIONS = {
    '1': 'Low',
    '2': 'Medium',
    '3': 'High'
}

INGREDIENT_SYNONYMS = {
    'chickpeas': 'garbanzo beans',
    'aubergine': 'eggplant',
    'courgette': 'zucchini',
    'capsicum': 'bell pepper',
    'spring onion': 'green onion',
    'coriander': 'cilantro',
}

COMMON_ALLERGENS = {
    'dairy': ['milk', 'cheese', 'butter', 'cream', 'yogurt'],
    'gluten': ['wheat', 'barley', 'rye', 'bread', 'pasta', 'flour'],
    'nuts': ['almond', 'peanut', 'cashew', 'walnut', 'hazelnut'],
    'soy': ['soy', 'tofu', 'soybean', 'soya'],
    'seafood': ['fish', 'shrimp', 'prawn', 'crab', 'lobster', 'shellfish']
}

INVENTORY_PROMPT_TEMPLATE = """
Analyze this refrigerator/kitchen image and list all food items you can identify.
Organize them by category (Fruits, Vegetables, Dairy, Meats, Beverages, Condiments, etc.).
For each item, include approximate quantity if visible (e.g. x2, half full).
Format as:

Category:
- Item (quantity or notes)
- Item (quantity or notes)

Next Category:
- etc.
"""

RECIPE_PROMPT_TEMPLATE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 AI CHEF ASSISTANT - SMART FRIDGE RECIPE GENERATION SYSTEM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ROLE: Professional Chef AI specialized in creating diverse, practical recipes

CRITICAL MISSION: Generate EXACTLY {num_recipes} COMPLETELY DISTINCT recipes
- Each recipe MUST use DIFFERENT ingredient combinations
- Each recipe MUST have a UNIQUE cooking approach
- NO FALLBACK CONTENT - Only AI-generated recipes allowed
- NO SHORTCUTS - Complete all {num_recipes} recipes fully

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📦 AVAILABLE INGREDIENTS IN USER'S FRIDGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{inventory}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 USER PROFILE & CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🚨 ALLERGIES (ABSOLUTE SAFETY REQUIREMENT): {allergies}
   → NEVER use these ingredients - This is NON-NEGOTIABLE
   
🕌 Cultural Restrictions: {cultural_restrictions}
   → STRICTLY respect (e.g., No Beef, Halal, No Pork, Jain)
   
🥗 Dietary Preferences: {dietary_restrictions}
   → Follow these guidelines for all recipes
   
👥 Household Size: {household_size} people
   → Adjust serving sizes accordingly
   
👶 Age Groups: {age_groups}
   → Adjust spice levels and complexity
   
🌍 Cuisine Preferences: {cuisine_preferences}
   → Prioritize these styles
   
💰 Budget Level: {budget}
   → Consider cost constraints

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 MANDATORY DIVERSITY STRATEGY (CRITICAL - READ CAREFULLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️  YOU MUST GENERATE {num_recipes} DISTINCT RECIPES ⚠️

DIVERSITY REQUIREMENTS:
1. Each recipe uses 2-4 MAIN ingredients from available inventory
2. Each recipe focuses on DIFFERENT primary ingredients
3. Maximum 1 ingredient overlap between any two recipes
4. Vary cooking methods (curry, stir-fry, sandwich, soup, etc.)
5. Vary cuisines when possible (Indian, Continental, Asian, etc.)

EXAMPLE DIVERSITY PATTERN:
Available: coconut, bread, brinjal, carrot, potato, chicken, cheese, milk

✅ Recipe 1: "Potato Carrot Curry"
   Uses: potato, carrot, milk
   Not Used: coconut, bread, brinjal, chicken, cheese
   
✅ Recipe 2: "Brinjal Coconut Stir-Fry"
   Uses: brinjal, coconut
   Not Used: bread, carrot, potato, chicken, cheese, milk
   
✅ Recipe 3: "Chicken Cheese Sandwich"
   Uses: chicken, bread, cheese
   Not Used: coconut, brinjal, carrot, potato, milk

NOTICE: Each recipe has DISTINCT main ingredients and cooking styles!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🛒 MISSING INGREDIENTS REQUIREMENT (NEW - CRITICAL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For EVERY recipe, you MUST list:
1. Ingredients FROM FRIDGE (mark with ✓)
2. Ingredients TO PURCHASE (missing from fridge)

This helps users create shopping lists!

EXAMPLE:
#### Ingredients:
- 2 medium potatoes ✓ (from your fridge)
- 1 large carrot ✓ (from your fridge)
- 1 cup milk ✓ (from your fridge)
- 2 tbsp vegetable oil (to purchase)
- 1 tsp cumin seeds (to purchase)
- 1/2 tsp turmeric powder (to purchase)
- Salt to taste (to purchase)

#### Missing Ingredients (Shopping List):
- Vegetable oil - 2 tbsp
- Cumin seeds - 1 tsp
- Turmeric powder - 1/2 tsp
- Salt - to taste

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
❌ FORBIDDEN PRACTICES (NEVER DO THESE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ Using ALL ingredients in a single recipe
❌ Creating "Mixed Everything" dishes
❌ Repeating same ingredient combinations across recipes
❌ Generating only 1 or 2 recipes when {num_recipes} requested
❌ Using allergens: {allergies}
❌ Violating cultural restrictions: {cultural_restrictions}
❌ Omitting the "Missing Ingredients" section
❌ Not marking fridge ingredients with ✓

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 MANDATORY OUTPUT FORMAT (Follow EXACTLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### Recipe: [Unique Recipe Name]

[1-2 line appetizing description]

#### Cuisine:
[Cuisine type - e.g., Indian, Continental, Asian]

#### Dietary Tags:
[e.g., Vegetarian, High-Protein, Gluten-Free, Kid-Friendly]

#### Ingredients:
- [quantity] [ingredient 1] ✓ (from your fridge)
- [quantity] [ingredient 2] ✓ (from your fridge)
- [quantity] [ingredient 3] (to purchase)
- [quantity] [ingredient 4] (to purchase)
[Continue for all ingredients needed]

#### Instructions:
1. [Clear, actionable first step]
2. [Second step]
3. [Third step]
4. [Continue with all steps needed]

#### Time:
- Prep: [X minutes]
- Cook: [X minutes]
- Total: [X minutes]

#### Serving:
- Servings: {household_size}
- Difficulty: [Easy/Medium/Hard]

#### Nutrition (per serving):
- Calories: [X kcal]
- Protein: [X g]
- Carbs: [X g]
- Fat: [X g]

#### Missing Ingredients (Shopping List):
- [ingredient 1] - [quantity]
- [ingredient 2] - [quantity]
- [ingredient 3] - [quantity]
[List ALL ingredients NOT available in fridge]

---END_RECIPE---

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ FINAL CHECKLIST (Verify Before Submitting)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Before submitting your response, verify:

✅ Generated EXACTLY {num_recipes} complete recipes
✅ Each recipe uses DIFFERENT main ingredients
✅ Each recipe has UNIQUE cooking method/style
✅ All fridge ingredients marked with ✓
✅ "Missing Ingredients" section included for EVERY recipe
✅ NO allergens used: {allergies}
✅ Cultural restrictions respected: {cultural_restrictions}
✅ Each recipe separated with "---END_RECIPE---"
✅ Realistic nutritional estimates provided
✅ Clear, numbered instructions for each recipe
✅ Shopping list helps user know what to buy

NOW GENERATE {num_recipes} AMAZING, DIVERSE RECIPES!
"""