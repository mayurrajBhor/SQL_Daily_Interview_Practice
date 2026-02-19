"""
Difficulty progression logic for SQL Daily Interview Practice Bot.
Determines next difficulty level based on user performance.
"""

from storage import get_recent_results, get_weak_topic, load_progress, update_current_difficulty

# Difficulty levels in order
DIFFICULTY_ORDER = ["beginner", "intermediate", "advanced"]


def get_next_difficulty(current: str, move: str) -> str:
    """
    Calculate next difficulty level.
    
    Args:
        current: Current difficulty level
        move: Direction to move ("up" or "down")
        
    Returns:
        New difficulty level
    """
    try:
        idx = DIFFICULTY_ORDER.index(current)
    except ValueError:
        # Invalid difficulty, default to beginner
        return "beginner"
    
    if move == "up" and idx < len(DIFFICULTY_ORDER) - 1:
        return DIFFICULTY_ORDER[idx + 1]
    elif move == "down" and idx > 0:
        return DIFFICULTY_ORDER[idx - 1]
    
    return current


def decide_difficulty(current_difficulty: str = None, user_id: int = None) -> str:
    """
    Decide the next difficulty level based on recent performance.
    
    Smart progression rules:
    - 3 correct in a row → Increase difficulty
    - 2+ incorrect in last 3 → Decrease difficulty
    - Mixed results → Stay at current level
    
    Args:
        current_difficulty: Current difficulty level (optional, will load from progress)
        user_id: User ID for multi-user support (required)
        
    Returns:
        Next difficulty level to use
    """
    if user_id is None:
        raise ValueError("user_id is required for decide_difficulty")
    
    # Load current difficulty from progress if not provided
    if current_difficulty is None:
        progress = load_progress(user_id)
        current_difficulty = progress.get("current_difficulty", "beginner")
    
    # Get recent results
    results = get_recent_results(user_id, limit=3)
    
    # Not enough data, stay at current level
    if len(results) < 3:
        return current_difficulty
    
    # 3 correct in a row → move up
    if results.count("Correct") == 3:
        new_difficulty = get_next_difficulty(current_difficulty, "up")
        update_current_difficulty(user_id, new_difficulty)
        return new_difficulty
    
    # 2 or more incorrect → move down
    if results.count("Incorrect") >= 2:
        new_difficulty = get_next_difficulty(current_difficulty, "down")
        update_current_difficulty(user_id, new_difficulty)
        return new_difficulty
    
    # Mixed results → stay same
    return current_difficulty
