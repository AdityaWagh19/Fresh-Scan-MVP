import os
import getpass
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List
from bcrypt import hashpw, gensalt, checkpw
from src.database.connection import DatabaseStateMachine , DatabaseConnectionContext
from src.database.transactions import MongoTransaction, TransactionManager
from src.database.connection import DatabaseConnectionError
from src.config.constants import (
    DIET_OPTIONS, ALLERGY_OPTIONS, CUISINE_OPTIONS, 
    PROTEIN_OPTIONS, AGE_GROUP_OPTIONS, CULTURAL_RESTRICTIONS,
    COOKING_FREQUENCY_OPTIONS, GROCERY_BUDGET_OPTIONS
)
from src.utils.helpers import (
    log_error, safe_db_call, get_multiple_choice, 
    input_with_default
)
from src.auth.validators import PasswordValidator, validate_password_with_feedback


class UserProfileManager:
    """Handles user authentication and profile management."""
    def __init__(self, database: DatabaseStateMachine):
        self.database = database
        self.current_user = None
        self.current_profile = None

    # Removed legacy login/register methods as per user request.
    # Authentication is now handled exclusively by NewAuthManager/OAuth.

    def run_onboarding_flow(self) -> None:
        """
        Execute mandatory new user onboarding flow.
        Collects essential profile data and secure mobile number.
        """
        if not self.current_user:
            return

        print("\n" + "="*60)
        print(" NEW USER ONBOARDING")
        print("="*60)
        
        print("\nWelcome to Smart Fridge!")
        print("To provide personalized recipes and seamless grocery ordering via Blinkit,")
        print("we need a few essential details. This is a one-time setup.")

        # Step 1: Basic Profile Information
        print("\n Step 1: Basic Profile Information")
        print("-" * 30)
        
        # Household Size
        household_size = 1
        while True:
            try:
                size_input = input("Household size (number of people) [1]: ").strip()
                if not size_input:
                    break
                size = int(size_input)
                if 1 <= size <= 20:
                    household_size = size
                    break
                print("Please enter a number between 1 and 20.")
            except ValueError:
                print("Invalid number.")

        # Age Groups
        print("\nPrimary age group(s):")
        age_groups = get_multiple_choice(
            "Select age groups",
            AGE_GROUP_OPTIONS
        ) or []

        # Cooking Frequency
        print("\nCooking Preferences:")
        cooking_freq_key = input("Cooking frequency (1: Daily, 2: Occasionally, 3: Rarely) [1]: ").strip() or "1"
        cooking_frequency = COOKING_FREQUENCY_OPTIONS.get(cooking_freq_key, "Daily")

        # Step 2: Dietary & Cultural Preferences
        print("\n🥗 Step 2: Dietary & Cultural Preferences")
        print("-" * 30)
        
        print("\nDietary Preferences:")
        diet_types = get_multiple_choice(
            "Diet type (select all that apply)",
            DIET_OPTIONS
        ) or []
        
        print("\nAllergies (if any):")
        allergies = get_multiple_choice(
            "Select allergies",
            ALLERGY_OPTIONS
        ) or []

        print("\nCultural / Religious Restrictions:")
        cultural_restrictions = get_multiple_choice(
            "Select restrictions",
            CULTURAL_RESTRICTIONS
        ) or []

        # Step 3: Cuisine Preferences
        print("\n🍽 Step 3: Cuisine Preferences")
        print("-" * 30)
        cuisine_preferences = get_multiple_choice(
            "Preferred Cuisines (select multiple)",
            CUISINE_OPTIONS
        ) or []

        # Step 4: Lifestyle Preferences
        print("\n💰 Step 4: Lifestyle Preferences")
        print("-" * 30)
        
        meals_per_day = 3
        while True:
            try:
                meals_input = input("Meals per day [3]: ").strip()
                if not meals_input:
                    break
                meals = int(meals_input)
                if 1 <= meals <= 10:
                    meals_per_day = meals
                    break
            except ValueError:
                pass
        
        print("\nPreferred protein sources:")
        proteins = get_multiple_choice(
            "Select protein sources",
            PROTEIN_OPTIONS
        ) or []

        print("\nMonthly grocery budget range:")
        budget_key = input("Budget (1: Low, 2: Medium, 3: High) [2]: ").strip() or "2"
        budget = GROCERY_BUDGET_OPTIONS.get(budget_key, "Medium").lower()

        # Step 5: Mobile Number (Mandatory)
        print("\n📱 Step 5: Mobile Number (Required for Blinkit Ordering)")
        print("-" * 30)
        print("This number will be used securely for Blinkit login and OTPs.")
        
        mobile_number = ""
        while True:
            mobile_number = input("Enter your mobile number (10 digits): ").strip()
            if mobile_number.isdigit() and len(mobile_number) == 10:
                break
            print("ERROR: Invalid number. Please enter a valid 10-digit mobile number.")

        # Save Profile
        try:
            profile_data = {
                "household_size": household_size,
                "age_groups": age_groups,
                "cooking_frequency": cooking_frequency,
                "diet_types": diet_types,
                "allergies": allergies,
                "cultural_restrictions": cultural_restrictions,
                "cuisine_preferences": cuisine_preferences,
                "meal_frequency": meals_per_day,
                "preferred_proteins": proteins,
                "budget": budget,
                "mobile_number": mobile_number  # Stored securely in profile
            }

            # Update database
            with DatabaseConnectionContext(self.database.get_client()) as db:
                db['users_v2'].update_one(
                    {"_id": self.current_user['_id']},
                    {
                        "$set": {
                            "profile": profile_data,
                            "is_onboarded": True,
                            "last_updated": datetime.now()
                        }
                    }
                )
            
            # Update local state
            self.current_profile = profile_data
            self.current_user['profile'] = profile_data
            
            print("\nOK: Confirmation Message")
            print("Thank you! Your profile has been successfully set up.")
            print("You can now enjoy:")
            print("- Personalized recipe recommendations")
            print("- Smart grocery lists")
            print("- One-tap Blinkit ordering")
            
            input("\nPress Enter to continue to the main menu...")

        except Exception as e:
            log_error("saving onboarding profile", e)
            print("\nERROR: Error saving profile. Please try editing it later.")

    def view_profile(self) -> None:
        """Display current user profile."""
        if not self.current_user:
            print("\nNo user logged in")
            return
           
        profile = self.current_profile
       
        print(f"\n{self.current_user['username']}'s Profile")
        
        # Household Information
        print("\nHousehold:")
        print(f"Size: {profile.get('household_size', 1)}")
        print(f"Age Groups: {', '.join(profile.get('age_groups', [])) or 'None'}")
        print(f"Cooking: {profile.get('cooking_frequency', 'Not specified')}")
        print(f"Shopping: {profile.get('shopping_frequency', 'Not specified')}")
       
        # Dietary Restrictions
        print("\nDietary:")
        print(f"Diets: {', '.join(profile.get('diet_types', [])) or 'None'}")
        print(f"Allergies: {', '.join(profile.get('allergies', [])) or 'None'}")
        print(f"Cultural: {', '.join(profile.get('cultural_restrictions', [])) or 'None'}")
       
        # Cuisine Preferences
        print("\nCuisines:")
        print(f"Preferred Cuisines: {', '.join(profile.get('cuisine_preferences', [])) or 'None'}")
       
        # Preferences
        print("\nPreferences:")
        print(f"Meals/Day: {profile.get('meal_frequency', 3)}")
        print(f"Proteins: {', '.join(profile.get('preferred_proteins', [])) or 'None'}")
        print(f"Budget: {profile.get('budget', 'medium').capitalize()}")

    def edit_profile(self) -> None:
        """Edit user profile with comprehensive options."""
        if not self.current_user:
            print("\nNo user logged in")
            return
           
        self.view_profile()
        
        print("\nEdit Profile")
        print("1. Household Information")
        print("2. Dietary Restrictions")
        print("3. Cuisine Preferences")
        print("4. Meal Preferences")
        print("5. Change Password (Forces Blinkit Re-login)")
        print("0. Back to Main Menu")
        
        choice = input("\nWhat would you like to edit? (0-5): ")
       
        try:
            if choice == '1':
                self._edit_household_info()
            elif choice == '2':
                self._edit_dietary_restrictions()
            elif choice == '3':
                self._edit_cuisine_preferences()
            elif choice == '4':
                self._edit_meal_preferences()
            elif choice == '5':
                self.change_password()
            elif choice == '0':
                return
            else:
                print("\nInvalid choice")
                return
               
            print("\nProfile updated successfully!")
        except Exception as e:
            log_error("profile edit", e)

    def _edit_household_info(self) -> None:
        """Edit household information section."""
        print("\nEdit Household Info")
       
        # Update household size
        new_size = input_with_default(
            f"Household size (current: {self.current_profile.get('household_size', 1)})",
            str(self.current_profile.get('household_size', 1))
        )
        if new_size:
            try:
                new_size = int(new_size)
                if 1 <= new_size <= 10:
                    self.current_profile['household_size'] = new_size
                    self._update_profile_field('household_size', new_size)
                else:
                    print("\nHousehold size must be between 1 and 10")
            except ValueError:
                print("\nPlease enter a valid number")
       
        # Update age groups
        age_groups = get_multiple_choice(
            f"Age groups (current: {', '.join(self.current_profile.get('age_groups', []))}",
            AGE_GROUP_OPTIONS
        )
        if age_groups is not None:
            self.current_profile['age_groups'] = age_groups
            self._update_profile_field('age_groups', age_groups)

    def _edit_dietary_restrictions(self) -> None:
        """Edit dietary restrictions section."""
        print("\nEdit Dietary Restrictions")
       
        # Update diet types
        diet_types = get_multiple_choice(
            f"Diet types (current: {', '.join(self.current_profile.get('diet_types', []))}",
            DIET_OPTIONS
        )
        if diet_types is not None:
            self.current_profile['diet_types'] = diet_types
            self._update_profile_field('diet_types', diet_types)
       
        # Update allergies
        allergies = get_multiple_choice(
            f"Allergies (current: {', '.join(self.current_profile.get('allergies', []))}",
            ALLERGY_OPTIONS
        )
        if allergies is not None:
            self.current_profile['allergies'] = allergies
            self._update_profile_field('allergies', allergies)

    def _edit_cuisine_preferences(self) -> None:
        """Edit cuisine preferences section."""
        print("\nEdit Cuisine Preferences")
       
        cuisines = get_multiple_choice(
            f"Preferred cuisines (current: {', '.join(self.current_profile.get('cuisine_preferences', []))}",
            CUISINE_OPTIONS
        )
        if cuisines is not None:
            self.current_profile['cuisine_preferences'] = cuisines
            self._update_profile_field('cuisine_preferences', cuisines)

    def _edit_meal_preferences(self) -> None:
        """Edit meal preferences section."""
        print("\nEdit Meal Preferences")
       
        # Update meal frequency
        meal_freq = input_with_default(
            f"Meal frequency (current: {self.current_profile.get('meal_frequency', 3)}):\n"
            "1. 3 main meals\n2. 3 meals + snacks\n3. 6 smaller meals",
            str(self.current_profile.get('meal_frequency', 3))
        )
        if meal_freq:
            new_freq = {"1": 3, "2": 4, "3": 6}.get(meal_freq, 3)
            self.current_profile['meal_frequency'] = new_freq
            self._update_profile_field('meal_frequency', new_freq)
       
        # Update proteins
        proteins = get_multiple_choice(
            f"Preferred proteins (current: {', '.join(self.current_profile.get('preferred_proteins', []))}",
            PROTEIN_OPTIONS
        )
        if proteins is not None:
            self.current_profile['preferred_proteins'] = proteins
            self._update_profile_field('preferred_proteins', proteins)
       
        # Update budget
        budget = input_with_default(
            f"Budget level (current: {self.current_profile.get('budget', 'medium')}):\n"
            "1. Low\n2. Medium\n3. High",
            {"low": "1", "medium": "2", "high": "3"}.get(self.current_profile.get('budget', 'medium'), "2")
        )
        if budget:
            new_budget = {"1": "low", "2": "medium", "3": "high"}.get(budget, "medium")
            self.current_profile['budget'] = new_budget
            self._update_profile_field('budget', new_budget)

    def change_password(self) -> None:
        """Change user password with session invalidation (Security hardening)."""
        if not self.current_user:
            return
            
        print("\n" + "="*50)
        print("CHANGE PASSWORD")
        print("="*50)
        
        try:
            old_password = getpass.getpass("Current Password: ")
            
            # Verify old password
            if not checkpw(old_password.encode('utf-8'), self.current_user['password']):
                print("Incorrect current password.")
                return
                
            print("\nEnter new password or press Enter to generate a secure one.")
            new_password = getpass.getpass("New Password: ")
            
            if not new_password:
                new_password = PasswordValidator.generate_secure_password()
                print(f"Generated secure password: {new_password}")
            
            # Validate new password
            is_valid, feedback = validate_password_with_feedback(new_password, self.current_user['username'])
            print(f"\n{feedback}")
            
            if not is_valid:
                print("Password change rejected.")
                return
                
            confirm = getpass.getpass("Confirm New Password: ")
            if new_password != confirm:
                print("Passwords do not match.")
                return
                
            # Update password in database using transaction
            hashed_pw = hashpw(new_password.encode('utf-8'), gensalt())
            txn_mgr = TransactionManager(self.database.get_client())
            
            def do_pw_change(txn: MongoTransaction):
                txn.update_one(
                    "users_v2",
                    {"_id": self.current_user['_id']},
                    {"$set": {"password_hash": hashed_pw}}
                )
                txn.insert_one("session_logs", {
                    "action": "password_changed",
                    "timestamp": datetime.now(),
                    "username": self.current_user['username']
                })
            
            txn_mgr.execute_in_transaction(do_pw_change)
            
            # Update in-memory user object
            self.current_user['password'] = hashed_pw
            print("\n Password updated successfully!")
            
            # CRITICAL: Invalidate current user's Blinkit session for safety
            print("Invalidating Blinkit sessions for security...")
            try:
                from src.services.integrations.blinkit import clear_user_blinkit_service
                asyncio.run(clear_user_blinkit_service(self.current_user['username']))
                
                from src.services.integrations.blinkit import get_session_manager
                session_mgr = get_session_manager()
                session_mgr.clear_session(self.current_user['username'])
                print("All active Blinkit sessions for this user have been cleared.")
            except Exception as e:
                log_error("clearing blinkit session after pw change", e)
                print("Warning: Failed to clear Blinkit sessions automatically.")
                
        except Exception as e:
            log_error("change password", e)
            print(f"Error changing password: {e}")

    def _update_profile_field(self, field: str, value: Any) -> None:
        """Helper to update a single profile field incrementally."""
        def update_field():
            with DatabaseConnectionContext(self.database.get_client()) as db:
                db['users_v2'].update_one(
                    {"_id": self.current_user['_id']},
                    {"$set": {f"profile.{field}": value}}
                )
        
        safe_db_call(f"update profile field {field}", update_field)