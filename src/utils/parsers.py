"""
Multi-Strategy Inventory Parser
Fixes brittle parsing with fallback strategies (Fix for Issue 1)
"""

import re
import json
import logging
from typing import List, Dict, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class ParsingResult:
    """Container for parsing results with metadata."""
    
    def __init__(self, items: List[Dict], dropped_lines: List[str], 
                 strategy: str, confidence: float):
        self.items = items
        self.dropped_lines = dropped_lines
        self.strategy = strategy
        self.confidence = confidence
    
    def to_dict(self) -> Dict:
        return {
            "items": self.items,
            "dropped_lines": self.dropped_lines,
            "strategy": strategy,
            "confidence": self.confidence,
            "items_count": len(self.items),
            "dropped_count": len(self.dropped_lines)
        }


def parse_inventory_multi_strategy(items_text: str, 
                                   normalize_func=None) -> ParsingResult:
    """
    Parse inventory with multiple fallback strategies.
    
    Strategies (in order):
    1. Dash-based (- Item, Category:)
    2. Numbered lists (1. Item, 2. Item)
    3. Comma-separated with categories
    4. JSON format
    5. Simple line-by-line
    
    Args:
        items_text: Raw text from AI
        normalize_func: Optional function to normalize ingredient names
    
    Returns:
        ParsingResult with items, dropped lines, strategy used, and confidence
    """
    # Try strategies in order
    strategies = [
        _parse_strategy_dash_based,
        _parse_strategy_numbered,
        _parse_strategy_comma_separated,
        _parse_strategy_json,
        _parse_strategy_simple_lines
    ]
    
    for strategy_func in strategies:
        try:
            result = strategy_func(items_text, normalize_func)
            if result.items:  # Success if we got any items
                logger.info(f"Parsing succeeded with strategy: {result.strategy}")
                return result
        except Exception as e:
            logger.debug(f"Strategy {strategy_func.__name__} failed: {e}")
            continue
    
    # All strategies failed
    logger.error("All parsing strategies failed")
    return ParsingResult(
        items=[],
        dropped_lines=items_text.split('\n'),
        strategy="none",
        confidence=0.0
    )


def _parse_strategy_dash_based(items_text: str, normalize_func) -> ParsingResult:
    """
    Strategy 1: Original dash-based format.
    
    Format:
        Category:
        - Item (notes)
        - Item (x2)
    """
    parsed_items = []
    dropped_lines = []
    current_category = None
    lines = items_text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Category detection
        if ':' in line and not line.startswith('-'):
            current_category = line.rstrip(':').strip()
            continue
        
        # Item detection
        if line.startswith('-') and current_category:
            item_info = {'category': current_category}
            item_text = line[1:].strip()
            
            # Parse quantity (x2, x3, etc.)
            if '(x' in item_text.lower():
                match = re.search(r'\(x(\d+)\)', item_text, re.IGNORECASE)
                if match:
                    item_name = item_text[:match.start()].strip()
                    item_info['quantity'] = match.group(1)
                else:
                    item_name = item_text
            # Parse notes in parentheses
            elif '(' in item_text and ')' in item_text:
                match = re.search(r'\((.*?)\)', item_text)
                item_name = item_text[:match.start()].strip()
                item_info['notes'] = match.group(1)
            else:
                item_name = item_text
            
            # Normalize name
            if normalize_func:
                item_info['name'] = normalize_func(item_name)
            else:
                item_info['name'] = item_name.lower().strip()
            
            item_info['timestamp'] = datetime.now()
            parsed_items.append(item_info)
        else:
            # Line doesn't match expected format
            if line and not line.startswith('#'):  # Ignore headers
                dropped_lines.append(line)
    
    # Calculate confidence based on success rate
    total_lines = len([l for l in lines if l.strip() and not l.strip().startswith('#')])
    confidence = len(parsed_items) / total_lines if total_lines > 0 else 0.0
    
    return ParsingResult(
        items=parsed_items,
        dropped_lines=dropped_lines,
        strategy="dash_based",
        confidence=confidence
    )


def _parse_strategy_numbered(items_text: str, normalize_func) -> ParsingResult:
    """
    Strategy 2: Numbered list format.
    
    Format:
        1. Item name (category: Fruits)
        2. Another item (category: Vegetables)
    """
    parsed_items = []
    dropped_lines = []
    lines = items_text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Match numbered items: "1. Item" or "1) Item"
        match = re.match(r'^(\d+)[.)]\s+(.+)$', line)
        if match:
            item_text = match.group(2)
            
            # Extract category if present
            category_match = re.search(r'\(category:\s*([^)]+)\)', item_text, re.IGNORECASE)
            if category_match:
                category = category_match.group(1).strip()
                item_name = item_text[:category_match.start()].strip()
            else:
                category = "Uncategorized"
                item_name = item_text
            
            # Parse quantity
            quantity_match = re.search(r'\(x(\d+)\)', item_name, re.IGNORECASE)
            quantity = quantity_match.group(1) if quantity_match else None
            if quantity_match:
                item_name = item_name[:quantity_match.start()].strip()
            
            item_info = {
                'category': category,
                'name': normalize_func(item_name) if normalize_func else item_name.lower().strip(),
                'timestamp': datetime.now()
            }
            if quantity:
                item_info['quantity'] = quantity
            
            parsed_items.append(item_info)
        else:
            if line and not line.startswith('#'):
                dropped_lines.append(line)
    
    confidence = len(parsed_items) / len(lines) if lines else 0.0
    
    return ParsingResult(
        items=parsed_items,
        dropped_lines=dropped_lines,
        strategy="numbered",
        confidence=confidence
    )


def _parse_strategy_comma_separated(items_text: str, normalize_func) -> ParsingResult:
    """
    Strategy 3: Comma-separated with categories.
    
    Format:
        Fruits: apple, banana, orange
        Vegetables: carrot, onion
    """
    parsed_items = []
    dropped_lines = []
    lines = items_text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line or not ':' in line:
            if line and not line.startswith('#'):
                dropped_lines.append(line)
            continue
        
        parts = line.split(':', 1)
        if len(parts) != 2:
            dropped_lines.append(line)
            continue
        
        category = parts[0].strip()
        items_str = parts[1].strip()
        
        # Split by comma
        items = [item.strip() for item in items_str.split(',')]
        
        for item_name in items:
            if item_name:
                item_info = {
                    'category': category,
                    'name': normalize_func(item_name) if normalize_func else item_name.lower().strip(),
                    'timestamp': datetime.now()
                }
                parsed_items.append(item_info)
    
    confidence = len(parsed_items) / len(lines) if lines else 0.0
    
    return ParsingResult(
        items=parsed_items,
        dropped_lines=dropped_lines,
        strategy="comma_separated",
        confidence=confidence
    )


def _parse_strategy_json(items_text: str, normalize_func) -> ParsingResult:
    """
    Strategy 4: JSON format detection.
    
    Format:
        [{"name": "apple", "category": "Fruits"}, ...]
        or
        {"Fruits": ["apple", "banana"], "Vegetables": ["carrot"]}
    """
    parsed_items = []
    dropped_lines = []
    
    try:
        data = json.loads(items_text)
        
        # Format 1: List of objects
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and 'name' in item:
                    item_info = {
                        'category': item.get('category', 'Uncategorized'),
                        'name': normalize_func(item['name']) if normalize_func else item['name'].lower().strip(),
                        'timestamp': datetime.now()
                    }
                    if 'quantity' in item:
                        item_info['quantity'] = str(item['quantity'])
                    if 'notes' in item:
                        item_info['notes'] = item['notes']
                    parsed_items.append(item_info)
        
        # Format 2: Dict of categories
        elif isinstance(data, dict):
            for category, items in data.items():
                if isinstance(items, list):
                    for item_name in items:
                        item_info = {
                            'category': category,
                            'name': normalize_func(item_name) if normalize_func else item_name.lower().strip(),
                            'timestamp': datetime.now()
                        }
                        parsed_items.append(item_info)
        
        confidence = 1.0 if parsed_items else 0.0
        
        return ParsingResult(
            items=parsed_items,
            dropped_lines=dropped_lines,
            strategy="json",
            confidence=confidence
        )
    
    except json.JSONDecodeError:
        # Not valid JSON
        return ParsingResult(items=[], dropped_lines=[], strategy="json", confidence=0.0)


def _parse_strategy_simple_lines(items_text: str, normalize_func) -> ParsingResult:
    """
    Strategy 5: Simple line-by-line (last resort).
    
    Format:
        apple
        banana
        carrot
    """
    parsed_items = []
    dropped_lines = []
    lines = items_text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or len(line) < 2:
            continue
        
        # Skip lines that look like headers
        if line.endswith(':') or line.isupper():
            continue
        
        # Remove leading markers
        line = re.sub(r'^[-*•]\s*', '', line)
        line = re.sub(r'^\d+[.)]\s*', '', line)
        
        if line:
            item_info = {
                'category': 'Uncategorized',
                'name': normalize_func(line) if normalize_func else line.lower().strip(),
                'timestamp': datetime.now()
            }
            parsed_items.append(item_info)
    
    confidence = 0.5  # Low confidence for simple parsing
    
    return ParsingResult(
        items=parsed_items,
        dropped_lines=dropped_lines,
        strategy="simple_lines",
        confidence=confidence
    )
