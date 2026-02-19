"""
Curriculum engine for SQL Daily Interview Practice Bot.
Handles topic recommendations, priority scoring, and mastery gating.
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from storage import load_progress, SQL_TOPICS_FILE

def load_topics_data() -> Dict[str, List[str]]:
    if not os.path.exists(SQL_TOPICS_FILE):
        return {}
    with open(SQL_TOPICS_FILE, "r") as f:
        return json.load(f)

def get_background_topics(target_topic: str) -> List[str]:
    """
    Returns a list of all topics that come before the target_topic in the hierarchy.
    Usage: To tell the LLM exactly what concepts the user already knows.
    """
    topics_data = load_topics_data()
    background = []
    
    # Flattened order of difficulties
    diff_order = ["beginner", "intermediate", "advanced"]
    
    found = False
    for diff in diff_order:
        if found: break
        for topic in topics_data.get(diff, []):
            if topic.lower() == target_topic.lower():
                found = True
                break
            background.append(topic)
            
    return background

def calculate_priority_score(topic: str, stats: Dict, difficulty: str) -> float:
    """
    Calculates a priority score for a topic. Higher = more important to practice.
    """
    score = 1.0
    correct = stats.get("correct", 0)
    incorrect = stats.get("incorrect", 0)
    total = correct + incorrect
    last_date_str = stats.get("last_practiced_at")
    
    # 1. New Topic Bonus (Very high priority for first 3 attempts)
    if total < 3:
        score += (5.0 - total) 
    
    # 2. Weakness Factor (Boost topics with low accuracy)
    if total >= 3:
        accuracy = correct / total
        if accuracy < 0.7:
            score += (1.0 - accuracy) * 4.0
            
    # 3. Recency / Spaced Repetition (Boost topics not seen in a while)
    if last_date_str:
        last_date = date.fromisoformat(last_date_str)
        days_since = (date.today() - last_date).days
        if days_since > 3:
            score += min(days_since * 0.5, 5.0) # Cap recency boost at 5
    else:
        # Never practiced
        score += 3.0

    return score

def check_mastery_gates(progress: Dict, topics_data: Dict[str, List[str]]) -> List[str]:
    """
    Returns a list of difficulties that are currently 'unlocked'.
    """
    unlocked = ["beginner"]
    
    # Calculate Beginner Mastery
    beg_stats = progress.get("difficulty_stats", {}).get("beginner", {"correct": 0, "incorrect": 0})
    beg_total = beg_stats["correct"] + beg_stats["incorrect"]
    beg_acc = beg_stats["correct"] / beg_total if beg_total > 0 else 0
    
    # Unlock Intermediate if Beginner has 5+ correct and > 70% accuracy
    if beg_stats["correct"] >= 5 and beg_acc >= 0.7:
        unlocked.append("intermediate")
        
        # Calculate Intermediate Mastery
        int_stats = progress.get("difficulty_stats", {}).get("intermediate", {"correct": 0, "incorrect": 0})
        int_total = int_stats["correct"] + int_stats["incorrect"]
        int_acc = int_stats["correct"] / int_total if int_total > 0 else 0
        
        # Unlock Advanced if Intermediate has 5+ correct and > 70% accuracy
        if int_stats["correct"] >= 5 and int_acc >= 0.7:
            unlocked.append("advanced")
            
    return unlocked

def get_mastery_progress(progress: Dict) -> Dict[str, Any]:
    """
    Calculates progress towards the next difficulty level unlock.
    """
    topics_data = load_topics_data()
    unlocked = check_mastery_gates(progress, topics_data)
    
    if "advanced" in unlocked:
        return {"status": "maxed", "message": "Maximum level reached! 🏆"}
        
    # Determine what we are aiming for
    current_level = "beginner"
    next_level = "intermediate"
    
    if "intermediate" in unlocked:
        current_level = "intermediate"
        next_level = "advanced"
        
    # Calculate stats for current level
    stats = progress.get("difficulty_stats", {}).get(current_level, {"correct": 0, "incorrect": 0})
    correct = stats.get("correct", 0)
    total = correct + stats.get("incorrect", 0)
    accuracy = (correct / total) if total > 0 else 0.0
    
    # Requirements
    REQ_CORRECT = 5
    REQ_ACCURACY = 0.7
    
    return {
        "status": "in_progress",
        "current_level": current_level,
        "next_level": next_level,
        "correct": correct,
        "correct_target": REQ_CORRECT,
        "accuracy": accuracy,
        "accuracy_target": REQ_ACCURACY
    }

def get_recommendation(user_id: int) -> Tuple[str, str, str]:
    """
    Returns (topic, difficulty, reason) for the next practice session.
    """
    progress = load_progress(user_id)
    topics_data = load_topics_data()
    unlocked_difficulties = check_mastery_gates(progress, topics_data)
    
    all_scored_topics = []
    
    for diff in unlocked_difficulties:
        topics_in_diff = topics_data.get(diff, [])
        for topic in topics_in_diff:
            # Get user's stats for this topic
            stats = progress.get("topic_stats", {}).get(topic, {})
            score = calculate_priority_score(topic, stats, diff)
            
            reason = ""
            if stats.get("correct", 0) + stats.get("incorrect", 0) < 3:
                reason = "Exploring new areas"
            elif stats.get("correct", 0) / (stats.get("correct", 0) + stats.get("incorrect", 0)) < 0.7:
                 reason = "Strengthening a weak spot"
            else:
                 reason = "Refreshing your memory"
                 
            all_scored_topics.append({
                "topic": topic,
                "difficulty": diff,
                "score": score,
                "reason": reason
            })
            
    # Sort by score descending
    all_scored_topics.sort(key=lambda x: x["score"], reverse=True)
    
    if not all_scored_topics:
        return "SELECT & FROM", "beginner", "Starting with basics"
        
    best = all_scored_topics[0]
    return best["topic"], best["difficulty"], best["reason"]

def get_available_topics(user_id: int) -> Dict[str, List[str]]:
    """
    Returns a dictionary of {difficulty: [topics]} that are currently unlocked for the user.
    """
    progress = load_progress(user_id)
    topics_data = load_topics_data()
    unlocked_diffs = check_mastery_gates(progress, topics_data)
    
    available = {}
    for diff in unlocked_diffs:
        available[diff] = topics_data.get(diff, [])
        
    return available
