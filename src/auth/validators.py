"""
Merged module from:
  - src/auth/password_validator.py
  - src/auth/profile_validator.py
  - src/auth/core/email_validator.py
"""

# Imports
from pydantic import BaseModel, Field, validator, ValidationError as PydanticValidationError
from typing import List, Dict, Optional, Any, Tuple
import re
import secrets
import string


# ===== From src/auth/password_validator.py =====

COMMON_PASSWORDS = {
    "password", "123456", "12345678", "qwerty", "abc123", "monkey",
    "1234567", "letmein", "trustno1", "dragon", "baseball", "111111",
    "iloveyou", "master", "sunshine", "ashley", "bailey", "passw0rd",
    "shadow", "123123", "654321", "superman", "qazwsx", "michael",
    "football", "welcome", "jesus", "ninja", "mustang", "password1",
    "123456789", "adobe123", "admin", "1234567890", "photoshop",
    "1234", "12345", "princess", "azerty", "000000", "access",
    "696969", "batman", "1qaz2wsx", "login", "qwertyuiop", "solo",
    "starwars", "whatever", "donald", "charlie", "aa123456", "freedom",
    "lovely", "7777777", "888888", "flower", "hottie", "loveme",
    "zaq1zaq1", "password123", "!@#$%^&*", "hello", "freedom",
    "computer", "121212", "123321", "1q2w3e4r", "secret", "123qwe",
    "test", "123abc", "password!", "qwerty123", "welcome123", "admin123"
}


class PasswordStrength:
    """Password strength levels."""
    VERY_WEAK = 0
    WEAK = 1
    MEDIUM = 2
    STRONG = 3
    VERY_STRONG = 4


class PasswordValidationResult:
    """Result of password validation."""
    
    def __init__(self, is_valid: bool, strength: int, 
                 errors: List[str], suggestions: List[str]):
        self.is_valid = is_valid
        self.strength = strength
        self.errors = errors
        self.suggestions = suggestions
    
    def get_strength_label(self) -> str:
        """Get human-readable strength label."""
        labels = {
            PasswordStrength.VERY_WEAK: "Very Weak",
            PasswordStrength.WEAK: "Weak",
            PasswordStrength.MEDIUM: "Medium",
            PasswordStrength.STRONG: "Strong",
            PasswordStrength.VERY_STRONG: "Very Strong"
        }
        return labels.get(self.strength, "Unknown")
    
    def to_dict(self) -> dict:
        """Export as dict."""
        return {
            "is_valid": self.is_valid,
            "strength": self.strength,
            "strength_label": self.get_strength_label(),
            "errors": self.errors,
            "suggestions": self.suggestions
        }


class PasswordValidator:
    """
    Validates password strength with comprehensive checks.
    
    Requirements:
    - Minimum 8 characters
    - At least 1 uppercase letter
    - At least 1 lowercase letter
    - At least 1 digit
    - At least 1 special character
    - Not in common passwords list
    """
    
    MIN_LENGTH = 8
    
    @staticmethod
    def validate(password: str, username: Optional[str] = None) -> PasswordValidationResult:
        """
        Validate password strength.
        
        Args:
            password: Password to validate
            username: Optional username to check for similarity
        
        Returns:
            PasswordValidationResult with validation details
        """
        errors = []
        suggestions = []
        strength = PasswordStrength.VERY_WEAK
        
        # Check minimum length
        if len(password) < PasswordValidator.MIN_LENGTH:
            errors.append(f"Password must be at least {PasswordValidator.MIN_LENGTH} characters")
            suggestions.append("Add more characters")
        
        # Check for uppercase
        if not re.search(r'[A-Z]', password):
            errors.append("Password must contain at least one uppercase letter")
            suggestions.append("Add uppercase letters (A-Z)")
        
        # Check for lowercase
        if not re.search(r'[a-z]', password):
            errors.append("Password must contain at least one lowercase letter")
            suggestions.append("Add lowercase letters (a-z)")
        
        # Check for digit
        if not re.search(r'\d', password):
            errors.append("Password must contain at least one digit")
            suggestions.append("Add numbers (0-9)")
        
        # Check for special character
        if not re.search(r'[!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|`~]', password):
            errors.append("Password must contain at least one special character")
            suggestions.append("Add special characters (!@#$%^&*)")
        
        # Check against common passwords
        if password.lower() in COMMON_PASSWORDS:
            errors.append("Password is too common")
            suggestions.append("Use a unique password")
        
        # Check for username similarity
        if username and username.lower() in password.lower():
            errors.append("Password should not contain username")
            suggestions.append("Avoid using your username")
        
        # Calculate strength
        if not errors:
            strength = PasswordValidator._calculate_strength(password)
            
            # Add suggestions based on strength
            if strength < PasswordStrength.STRONG:
                if len(password) < 12:
                    suggestions.append("Use 12+ characters for better security")
                if not re.search(r'[!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|`~]{2,}', password):
                    suggestions.append("Use multiple special characters")
        
        is_valid = len(errors) == 0
        
        return PasswordValidationResult(
            is_valid=is_valid,
            strength=strength,
            errors=errors,
            suggestions=suggestions
        )
    
    @staticmethod
    def _calculate_strength(password: str) -> int:
        """
        Calculate password strength (0-4).
        
        Factors:
        - Length
        - Character variety
        - Patterns
        """
        score = 0
        
        # Length scoring
        if len(password) >= 8:
            score += 1
        if len(password) >= 12:
            score += 1
        if len(password) >= 16:
            score += 1
        
        # Character variety
        has_upper = bool(re.search(r'[A-Z]', password))
        has_lower = bool(re.search(r'[a-z]', password))
        has_digit = bool(re.search(r'\d', password))
        has_special = bool(re.search(r'[!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|`~]', password))
        
        variety_count = sum([has_upper, has_lower, has_digit, has_special])
        if variety_count >= 3:
            score += 1
        if variety_count == 4:
            score += 1
        
        # Penalize patterns
        if re.search(r'(.)\1{2,}', password):  # Repeated characters
            score -= 1
        if re.search(r'(012|123|234|345|456|567|678|789|890)', password):  # Sequential numbers
            score -= 1
        if re.search(r'(abc|bcd|cde|def|efg|fgh|ghi|hij|ijk|jkl|klm|lmn|mno|nop|opq|pqr|qrs|rst|stu|tuv|uvw|vwx|wxy|xyz)', password.lower()):  # Sequential letters
            score -= 1
        
        # Clamp to 0-4 range
        return max(0, min(4, score))
    
    @staticmethod
    def generate_secure_password(length: int = 16) -> str:
        """
        Generate a cryptographically secure random password.
        
        Args:
            length: Password length (default: 16)
        
        Returns:
            Secure random password
        """
        if length < PasswordValidator.MIN_LENGTH:
            length = PasswordValidator.MIN_LENGTH
        
        # Ensure at least one of each required character type
        password_chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice(string.punctuation)
        ]
        
        # Fill the rest with random characters
        all_chars = string.ascii_letters + string.digits + string.punctuation
        password_chars.extend(secrets.choice(all_chars) for _ in range(length - 4))
        
        # Shuffle to avoid predictable pattern
        password_list = list(password_chars)
        secrets.SystemRandom().shuffle(password_list)
        
        return ''.join(password_list)
    
    @staticmethod
    def get_strength_color(strength: int) -> str:
        """Get color code for strength level (for UI)."""
        colors = {
            PasswordStrength.VERY_WEAK: "red",
            PasswordStrength.WEAK: "orange",
            PasswordStrength.MEDIUM: "yellow",
            PasswordStrength.STRONG: "lightgreen",
            PasswordStrength.VERY_STRONG: "green"
        }
        return colors.get(strength, "gray")


def validate_password_with_feedback(password: str, username: Optional[str] = None) -> Tuple[bool, str]:
    """
    Validate password and return user-friendly feedback.
    
    Args:
        password: Password to validate
        username: Optional username
    
    Returns:
        Tuple of (is_valid, feedback_message)
    """
    result = PasswordValidator.validate(password, username)
    
    if result.is_valid:
        feedback = f"Password strength: {result.get_strength_label()}"
        if result.suggestions:
            feedback += f"\nSuggestions: {', '.join(result.suggestions)}"
        return True, feedback
    else:
        feedback = "Password validation failed:\n"
        for error in result.errors:
            feedback += f"  - {error}\n"
        if result.suggestions:
            feedback += f"\nSuggestions:\n"
            for suggestion in result.suggestions:
                feedback += f"  - {suggestion}\n"
        return False, feedback

# ===== From src/auth/profile_validator.py =====

from src.config.constants import (
    DIET_OPTIONS, ALLERGY_OPTIONS, CUISINE_OPTIONS,
    PROTEIN_OPTIONS, AGE_GROUP_OPTIONS, CULTURAL_RESTRICTIONS
)


class ValidationError(Exception):
    """Custom validation error with field-level details."""
    
    def __init__(self, errors: Dict[str, List[str]]):
        self.errors = errors
        super().__init__(self._format_errors())
    
    def _format_errors(self) -> str:
        """Format errors for display."""
        lines = ["Validation failed:"]
        for field, field_errors in self.errors.items():
            for error in field_errors:
                lines.append(f"  - {field}: {error}")
        return "\n".join(lines)
    
    def get_field_errors(self, field: str) -> List[str]:
        """Get errors for a specific field."""
        return self.errors.get(field, [])


class HouseholdInfo(BaseModel):
    """Household information validation schema."""
    
    household_size: int = Field(ge=1, le=10, description="Number of people (1-10)")
    age_groups: List[str] = Field(default_factory=list)
    cooking_frequency: str = Field(default="Mixed")
    shopping_frequency: str = Field(default="Weekly")
    
    @validator('age_groups')
    def validate_age_groups(cls, v):
        """Validate age groups are from allowed options."""
        if not v:
            return v
        
        # Get allowed values (handle both dict and list)
        allowed = list(AGE_GROUP_OPTIONS.values()) if isinstance(AGE_GROUP_OPTIONS, dict) else AGE_GROUP_OPTIONS
        
        invalid = [age for age in v if age not in allowed]
        if invalid:
            raise ValueError(f"Invalid age groups: {', '.join(invalid)}")
        return v
    
    @validator('cooking_frequency')
    def validate_cooking_frequency(cls, v):
        """Validate cooking frequency."""
        allowed = ["Daily", "Batch", "Mixed"]
        if v not in allowed:
            raise ValueError(f"Must be one of: {', '.join(allowed)}")
        return v
    
    @validator('shopping_frequency')
    def validate_shopping_frequency(cls, v):
        """Validate shopping frequency."""
        allowed = ["Weekly", "Bi-weekly", "Monthly"]
        if v not in allowed:
            raise ValueError(f"Must be one of: {', '.join(allowed)}")
        return v


class DietaryRestrictions(BaseModel):
    """Dietary restrictions validation schema."""
    
    diet_types: List[str] = Field(default_factory=list)
    allergies: List[str] = Field(default_factory=list)
    cultural_restrictions: List[str] = Field(default_factory=list)
    
    @validator('diet_types')
    def validate_diet_types(cls, v):
        """Validate diet types are from allowed options."""
        if not v:
            return v
        
        invalid = [diet for diet in v if diet not in DIET_OPTIONS]
        if invalid:
            raise ValueError(f"Invalid diet types: {', '.join(invalid)}")
        return v
    
    @validator('allergies')
    def validate_allergies(cls, v):
        """Validate allergies (from options or custom with max length 50)."""
        if not v:
            return v
        
        for allergy in v:
            # Check if from predefined options
            if allergy in ALLERGY_OPTIONS:
                continue
            
            # Allow custom allergies with max length 50
            if len(allergy) > 50:
                raise ValueError(f"Custom allergy '{allergy}' exceeds 50 characters")
            
            # Must be alphanumeric with spaces/hyphens
            if not re.match(r'^[a-zA-Z0-9\s\-]+$', allergy):
                raise ValueError(f"Invalid allergy format: '{allergy}'")
        
        return v
    
    @validator('cultural_restrictions')
    def validate_cultural_restrictions(cls, v):
        """Validate cultural restrictions."""
        if not v:
            return v
        
        invalid = [cr for cr in v if cr not in CULTURAL_RESTRICTIONS]
        if invalid:
            raise ValueError(f"Invalid cultural restrictions: {', '.join(invalid)}")
        return v


class MealPreferences(BaseModel):
    """Meal preferences validation schema."""
    
    meal_frequency: int = Field(ge=1, le=10, description="Meals per day (1-10)")
    preferred_proteins: List[str] = Field(default_factory=list)
    cuisine_preferences: List[str] = Field(default_factory=list)
    budget: str = Field(default="medium")
    
    @validator('preferred_proteins')
    def validate_proteins(cls, v):
        """Validate proteins are from allowed options."""
        if not v:
            return v
        
        invalid = [p for p in v if p not in PROTEIN_OPTIONS]
        if invalid:
            raise ValueError(f"Invalid proteins: {', '.join(invalid)}")
        return v
    
    @validator('cuisine_preferences')
    def validate_cuisines(cls, v):
        """Validate cuisines are from allowed options."""
        if not v:
            return v
        
        invalid = [c for c in v if c not in CUISINE_OPTIONS]
        if invalid:
            raise ValueError(f"Invalid cuisines: {', '.join(invalid)}")
        return v
    
    @validator('budget')
    def validate_budget(cls, v):
        """Validate budget level."""
        allowed = ["low", "medium", "high"]
        if v.lower() not in allowed:
            raise ValueError(f"Must be one of: {', '.join(allowed)}")
        return v.lower()


class UserProfile(BaseModel):
    """Complete user profile validation schema."""
    
    household_size: int = Field(ge=1, le=10)
    age_groups: List[str] = Field(default_factory=list)
    cooking_frequency: str = Field(default="Mixed")
    shopping_frequency: str = Field(default="Weekly")
    diet_types: List[str] = Field(default_factory=list)
    allergies: List[str] = Field(default_factory=list)
    cultural_restrictions: List[str] = Field(default_factory=list)
    cuisine_preferences: List[str] = Field(default_factory=list)
    meal_frequency: int = Field(ge=1, le=10)
    preferred_proteins: List[str] = Field(default_factory=list)
    budget: str = Field(default="medium")
    
    @validator('household_size')
    def validate_household_size(cls, v):
        if not 1 <= v <= 10:
            raise ValueError("Must be between 1 and 10")
        return v
    
    @validator('meal_frequency')
    def validate_meal_frequency(cls, v):
        if not 1 <= v <= 10:
            raise ValueError("Must be between 1 and 10")
        return v
    
    @validator('age_groups')
    def validate_age_groups(cls, v):
        invalid = [age for age in v if age not in AGE_GROUP_OPTIONS]
        if invalid:
            raise ValueError(f"Invalid age groups: {', '.join(invalid)}")
        return v
    
    @validator('allergies')
    def validate_allergies(cls, v):
        for allergy in v:
            if allergy not in ALLERGY_OPTIONS and len(allergy) > 50:
                raise ValueError(f"Custom allergy '{allergy}' exceeds 50 characters")
        return v


class ProfileValidator:
    """Validates user profile data with detailed error reporting."""
    
    @staticmethod
    def validate_household_info(data: Dict[str, Any]) -> None:
        """
        Validate household information.
        
        Args:
            data: Dict with household_size, age_groups, etc.
        
        Raises:
            ValidationError: If validation fails
        """
        try:
            HouseholdInfo(**data)
        except PydanticValidationError as e:
            errors = {}
            for error in e.errors():
                field = error['loc'][0]
                msg = error['msg']
                if field not in errors:
                    errors[field] = []
                errors[field].append(msg)
            raise ValidationError(errors)
    
    @staticmethod
    def validate_dietary_restrictions(data: Dict[str, Any]) -> None:
        """
        Validate dietary restrictions.
        
        Args:
            data: Dict with diet_types, allergies, cultural_restrictions
        
        Raises:
            ValidationError: If validation fails
        """
        try:
            DietaryRestrictions(**data)
        except PydanticValidationError as e:
            errors = {}
            for error in e.errors():
                field = error['loc'][0]
                msg = error['msg']
                if field not in errors:
                    errors[field] = []
                errors[field].append(msg)
            raise ValidationError(errors)
    
    @staticmethod
    def validate_meal_preferences(data: Dict[str, Any]) -> None:
        """
        Validate meal preferences.
        
        Args:
            data: Dict with meal_frequency, proteins, cuisines, budget
        
        Raises:
            ValidationError: If validation fails
        """
        try:
            MealPreferences(**data)
        except PydanticValidationError as e:
            errors = {}
            for error in e.errors():
                field = error['loc'][0]
                msg = error['msg']
                if field not in errors:
                    errors[field] = []
                errors[field].append(msg)
            raise ValidationError(errors)
    
    @staticmethod
    def validate_complete_profile(data: Dict[str, Any]) -> None:
        """
        Validate complete user profile.
        
        Args:
            data: Complete profile dict
        
        Raises:
            ValidationError: If validation fails
        """
        try:
            UserProfile(**data)
        except PydanticValidationError as e:
            errors = {}
            for error in e.errors():
                field = error['loc'][0]
                msg = error['msg']
                if field not in errors:
                    errors[field] = []
                errors[field].append(msg)
            raise ValidationError(errors)
    
    @staticmethod
    def validate_field(field_name: str, value: Any, profile_type: str = "complete") -> None:
        """
        Validate a single field.
        
        Args:
            field_name: Name of the field
            value: Value to validate
            profile_type: Type of profile section (household, dietary, meal, complete)
        
        Raises:
            ValidationError: If validation fails
        """
        # Create minimal dict with just this field
        data = {field_name: value}
        
        # Add required fields with defaults
        if profile_type == "complete":
            data.setdefault('household_size', 1)
            data.setdefault('meal_frequency', 3)
        
        try:
            if profile_type == "household":
                HouseholdInfo(**data)
            elif profile_type == "dietary":
                DietaryRestrictions(**data)
            elif profile_type == "meal":
                MealPreferences(**data)
            else:
                UserProfile(**data)
        except PydanticValidationError as e:
            errors = {}
            for error in e.errors():
                if error['loc'][0] == field_name:
                    if field_name not in errors:
                        errors[field_name] = []
                    errors[field_name].append(error['msg'])
            if errors:
                raise ValidationError(errors)

# ===== From src/auth/core/email_validator.py =====

EMAIL_REGEX = re.compile(
    r'^[a-zA-Z0-9.!#$%&\'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$'
)


def validate_email(email: str) -> Tuple[bool, str]:
    """
    Validate email address according to RFC 5322.
    
    Args:
        email: Email address to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not email:
        return False, "Email is required"
    
    # Check length
    if len(email) > 254:
        return False, "Email is too long (max 254 characters)"
    
    # Check format
    if not EMAIL_REGEX.match(email):
        return False, "Invalid email format"
    
    # Split local and domain parts
    try:
        local, domain = email.rsplit('@', 1)
    except ValueError:
        return False, "Invalid email format"
    
    # Validate local part
    if len(local) > 64:
        return False, "Email local part is too long (max 64 characters)"
    
    if not local:
        return False, "Email local part cannot be empty"
    
    # Validate domain part
    if len(domain) > 253:
        return False, "Email domain is too long (max 253 characters)"
    
    if not domain:
        return False, "Email domain cannot be empty"
    
    # Check for consecutive dots
    if '..' in email:
        return False, "Email cannot contain consecutive dots"
    
    # Check domain has at least one dot
    if '.' not in domain:
        return False, "Email domain must contain at least one dot"
    
    return True, ""


def normalize_email(email: str) -> str:
    """
    Normalize email address to lowercase.
    
    Args:
        email: Email address
        
    Returns:
        Normalized email address
    """
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    """
    Quick check if email is valid.
    
    Args:
        email: Email address
        
    Returns:
        True if valid, False otherwise
    """
    is_valid, _ = validate_email(email)
    return is_valid