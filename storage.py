import json
import os
import uuid
import datetime
import requests
from pathlib import Path
from datetime import date, datetime
from typing import Dict, List, Optional, Any
from dotenv import load_dotenv
from db import get_db_connection, init_db

# Load env
load_dotenv()

# ===================== CONFIG =====================
GH_TOKEN = os.getenv("GH_TOKEN")
GH_REPO = os.getenv("GH_REPO") 
SQL_TOPICS_FILE = "sql_topics.json" # Keep static config as file

# ===================== CONSTANTS =====================
# For single-user compatibility mode, we map everything to one main user.
# In a future multi-user update, we can accept chat_id in load_progress.
DEFAULT_USER_ID = 837013855  # Real Telegram chat ID
DEFAULT_USERNAME = "Admin"

# ===================== INITIALIZATION =====================

def init_storage():
    """Ensure database tables exist."""
    print("📦 Initializing storage...")
    init_db()

# ===================== ATTEMPTS MANAGEMENT =====================

def load_attempts_history(user_id: int) -> List[Dict]:
    """Load all historical attempts from DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Detect Column Name
    id_col = get_user_id_column(cur)
    
    cur.execute(f"""
        SELECT question_id, difficulty, topic, question_text as question, 
               user_answer, result, explanation, timestamp
        FROM attempts 
        WHERE {id_col} = %s
        ORDER BY timestamp ASC
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    # Convert rows to dicts if not already (RealDictCursor handles this)
    # Just need to ensure date formatting if needed, but JSON serializer usually handles strings.
    # We might need to convert datetime objects to string for compatibility with existing code expectation (which expects JSON-like dicts).
    history = []
    for row in rows:
        r = dict(row)
        if isinstance(r.get("timestamp"), datetime):
            r["timestamp"] = r["timestamp"].isoformat()
        history.append(r)
    return history

def save_attempt(user_id: int, attempt_data: Dict) -> None:
    """Save a new attempt to DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if we need to create the user first?
    ensure_user_exists(user_id)
    
    timestamp = datetime.now()
    id_col = get_user_id_column(cur)
    
    cur.execute(f"""
        INSERT INTO attempts ({id_col}, question_id, difficulty, topic, question_text, user_answer, result, explanation, retry_count, challenge_id, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        user_id,
        attempt_data.get("question_id"),
        attempt_data.get("difficulty"),
        attempt_data.get("topic"),
        attempt_data.get("question"),
        attempt_data.get("user_answer"),
        attempt_data.get("result"),
        attempt_data.get("explanation"),
        attempt_data.get("retry_count", 0),
        attempt_data.get("challenge_id"),
        timestamp
    ))
    
    conn.commit()
    cur.close()
    conn.close()
    
    # Update progress logic (which writes to users/topic_stats)
    update_progress(user_id, attempt_data["result"], attempt_data["difficulty"], attempt_data["topic"])

def is_question_evaluated(question_id: str) -> bool:
    """Check if a question has already been evaluated."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM attempts WHERE question_id = %s", (question_id,))
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists

# ===================== PROGRESS TRACKING =====================

def load_progress(user_id: int) -> Dict:
    """Load user progress statistics from DB and format as legacy JSON structure."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    id_col = get_user_id_column(cur)
    
    # 1. Get User Stats
    cur.execute(f"""
        SELECT current_difficulty, total_correct, total_incorrect, streak_days, last_activity_date
        FROM users WHERE {id_col} = %s
    """, (user_id,))
    user_row = cur.fetchone()
    
    if not user_row:
        # Return default structure if user doesn't exist
        return {
            "current_difficulty": "beginner",
            "total_correct": 0,
            "total_incorrect": 0,
            "streak_days": 0,
            "last_activity_date": None,
            "difficulty_stats": {
                "beginner": {"correct": 0, "incorrect": 0},
                "intermediate": {"correct": 0, "incorrect": 0},
                "advanced": {"correct": 0, "incorrect": 0}
            },
            "topic_stats": {}
        }
    
    progress = dict(user_row)
    # Fix date serialization
    if progress["last_activity_date"]:
        progress["last_activity_date"] = progress["last_activity_date"].isoformat()
    
    # 2. Get Topic Stats
    topic_stats = {}
    cur.execute(f"SELECT topic_name, correct_count, incorrect_count, last_practiced_at, current_level FROM topic_stats WHERE {id_col} = %s", (user_id,))
    t_rows = cur.fetchall()
    
    for row in t_rows:
        last_prac = row["last_practiced_at"].isoformat() if row["last_practiced_at"] else None
        topic_stats[row["topic_name"]] = {
            "correct": row["correct_count"],
            "incorrect": row["incorrect_count"],
            "last_practiced_at": last_prac,
            "current_level": row.get("current_level", 1)
        }
    
    progress["topic_stats"] = topic_stats
    
    difficulty_stats = {
        "beginner": {"correct": 0, "incorrect": 0},
        "intermediate": {"correct": 0, "incorrect": 0},
        "advanced": {"correct": 0, "incorrect": 0}
    }
    
    # Query aggregates
    cur.execute(f"""
        SELECT difficulty, result, COUNT(*) as count
        FROM attempts
        WHERE {id_col} = %s
        GROUP BY difficulty, result
    """, (user_id,))
    agg_rows = cur.fetchall()
    
    for row in agg_rows:
        diff = row["difficulty"]
        res = row["result"] # "Correct" or "Incorrect"
        cnt = row["count"]
        
        if diff in difficulty_stats:
            key = "correct" if res == "Correct" else "incorrect"
            difficulty_stats[diff][key] = cnt
            
    progress["difficulty_stats"] = difficulty_stats
    
    cur.close()
    conn.close()
    
    return progress

def update_progress(user_id: int, result: str, difficulty: str, topic: str) -> None:
    """Update progress stats in DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    ensure_user_exists(user_id)
    id_col = get_user_id_column(cur)
    
    # Update User Totals
    if result == "Correct":
        cur.execute(f"UPDATE users SET total_correct = total_correct + 1 WHERE {id_col} = %s", (user_id,))
    else:
        cur.execute(f"UPDATE users SET total_incorrect = total_incorrect + 1 WHERE {id_col} = %s", (user_id,))
    
    # Update Topic Stats
    today = date.today()
    cur.execute(f"""
        INSERT INTO topic_stats ({id_col}, topic_name, correct_count, incorrect_count, last_practiced_at)
        VALUES (%s, %s, 0, 0, %s)
        ON CONFLICT ({id_col}, topic_name) DO UPDATE 
        SET last_practiced_at = EXCLUDED.last_practiced_at;
    """, (user_id, topic, today))
    
    if result == "Correct":
        cur.execute(f"""
            UPDATE topic_stats 
            SET correct_count = correct_count + 1,
                current_level = LEAST(current_level + 1, 3) 
            WHERE {id_col} = %s AND topic_name = %s
        """, (user_id, topic))
    else:
        cur.execute(f"UPDATE topic_stats SET incorrect_count = incorrect_count + 1 WHERE {id_col} = %s AND topic_name = %s", (user_id, topic))
    
    # Update Daily Question Counter
    cur.execute(f"SELECT daily_questions_count, last_reset_date FROM users WHERE {id_col} = %s", (user_id,))
    row = cur.fetchone()
    daily_count = row["daily_questions_count"] if row else 0
    last_reset = row["last_reset_date"] if row else None
    
    # Reset counter if it's a new day
    if last_reset != today:
        daily_count = 0
    
    # Increment counter if answer was correct
    if result == "Correct":
        daily_count += 1
    
    cur.execute(
        f"UPDATE users SET daily_questions_count = %s, last_reset_date = %s WHERE {id_col} = %s",
        (daily_count, today, user_id)
    )
        
    # Update Streak logic
    # Fetch current state first to replicate complex streak logic
    cur.execute(f"SELECT streak_days, last_activity_date FROM users WHERE {id_col} = %s", (user_id,))
    row = cur.fetchone()
    current_streak = row["streak_days"]
    last_act = row["last_activity_date"] # date object
    
    new_streak = current_streak
    
    if last_act == today:
        pass # Already active today
    elif last_act:
        from datetime import timedelta
        yesterday = today - timedelta(days=1)
        if last_act == yesterday and result == "Correct":
            new_streak += 1
        elif result == "Correct":
            new_streak = 1
        else:
            new_streak = 0
    else:
        new_streak = 1 if result == "Correct" else 0
        
    cur.execute(f"UPDATE users SET streak_days = %s, last_activity_date = %s WHERE {id_col} = %s", (new_streak, today, user_id))
    
    conn.commit()
    cur.close()
    conn.close()

def update_current_difficulty(user_id: int, new_difficulty: str) -> None:
    """Update user's current difficulty."""
    conn = get_db_connection()
    cur = conn.cursor()
    ensure_user_exists(user_id)
    id_col = get_user_id_column(cur)
    cur.execute(f"UPDATE users SET current_difficulty = %s WHERE {id_col} = %s", (new_difficulty, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_daily_progress(user_id: int) -> Dict:
    """Get user's daily mission progress (questions solved today out of 5)."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    today = date.today()
    id_col = get_user_id_column(cur)
    
    cur.execute(
        f"SELECT daily_questions_count, last_reset_date FROM users WHERE {id_col} = %s",
        (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if not row:
        return {"completed": 0, "target": 2, "is_complete": False}
    
    daily_count = row["daily_questions_count"] or 0
    last_reset = row["last_reset_date"]
    
    # Reset if it's a new day
    if last_reset != today:
        daily_count = 0
    
    return {
        "completed": daily_count,
        "target": 2,
        "is_complete": daily_count >= 2
    }

def get_users_pending_mission():
    """Get list of users who haven't completed their daily mission."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    today = date.today()
    
    # 1. Detect Column Name
    id_col = get_user_id_column(cur)
    
    # Find users who haven't reached 2 questions today.
    cur.execute(f"""
        SELECT {id_col} as user_id, 
               CASE WHEN last_reset_date = %s THEN daily_questions_count ELSE 0 END as daily_questions_count
        FROM users 
        WHERE (last_reset_date < %s OR last_reset_date IS NULL)
           OR (last_reset_date = %s AND daily_questions_count < 2)
    """, (today, today, today))
    
    users = cur.fetchall()
    cur.close()
    conn.close()
    
    return users

# ===================== QUESTION HELPERS =====================

def load_last_question() -> Dict:
    """Load the most recent daily question."""
    conn = get_db_connection()
    cur = conn.cursor()
    today = date.today()
    
    # Get the LATEST question for today (multiple quizzes allowed)
    # Using created_at to sort
    cur.execute("""
        SELECT * FROM daily_questions 
        WHERE date = %s 
        ORDER BY created_at DESC 
        LIMIT 1
    """, (today,))
    
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if row:
        d = dict(row)
        if d.get("date"): d["date"] = d["date"].isoformat()
        if d.get("created_at"): d["created_at"] = d["created_at"].isoformat()
        # Map 'question_text' back to 'question' for legacy compatibility
        d["question"] = d.get("question_text", "")
        return d
        
    return {
        "question_id": "initial",
        "date": date.today().isoformat(),
        "difficulty": "beginner",
        "topic": "SELECT",
        "question": "No question sent yet.",
        "retry_count": 0
    }

def get_past_questions(topic: str = "all", limit: int = 15) -> List[str]:
    """Fetch recent question texts to avoid duplicates. Use topic='all' for global history."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    if topic == "all":
        cur.execute("""
            SELECT question_text FROM daily_questions 
            ORDER BY created_at DESC 
            LIMIT %s
        """, (limit,))
    else:
        cur.execute("""
            SELECT question_text FROM daily_questions 
            WHERE topic = %s 
            ORDER BY created_at DESC 
            LIMIT %s
        """, (topic, limit))
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    questions = []
    for row in rows:
        q_text = row["question_text"]
        # Basic check: if it looks like JSON, try to extract just the 'question' field
        if q_text.strip().startswith("{"):
            try:
                import json
                data = json.loads(q_text)
                if "question" in data:
                    questions.append(data["question"])
                    continue
            except:
                pass
        questions.append(q_text)
        
    return questions

def save_last_question(question_data: Dict) -> None:
    """Save daily question."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Removed ON CONFLICT to allow multiple questions per day
    cur.execute("""
        INSERT INTO daily_questions (date, question_id, difficulty, topic, question_text, retry_count)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        question_data.get("date"),
        question_data.get("question_id"),
        question_data.get("difficulty"),
        question_data.get("topic"),
        question_data.get("question"),
        question_data.get("retry_count", 0)
    ))
    conn.commit()
    cur.close()
    conn.close()



def get_leaderboard_data(limit=10):
    """Fetch top users based on weighted points for the CURRENT WEEK (Mon-Sun)."""
    conn = get_db_connection()
    cur = conn.cursor()
    id_col = get_user_id_column(cur)
    
    cur.execute(f"""
        WITH QuizPoints AS (
            SELECT user_id, COUNT(*) * 10 as pts
            FROM quiz_answers
            WHERE is_correct = TRUE 
              AND created_at >= date_trunc('week', CURRENT_TIMESTAMP)
            GROUP BY user_id
        ),
        CodingPoints AS (
            /* Only count the first successful attempt per question this week */
            WITH FirstSuccess AS (
                SELECT {id_col} as user_id, question_id, MIN(timestamp) as first_correct_ts
                FROM attempts
                WHERE result = 'Correct'
                  AND timestamp >= date_trunc('week', CURRENT_TIMESTAMP)
                GROUP BY {id_col}, question_id
            ),
            AttemptCounts AS (
                SELECT 
                    fs.user_id,
                    fs.question_id,
                    COUNT(a.id) as prev_failures
                FROM FirstSuccess fs
                LEFT JOIN attempts a ON fs.user_id = a.{id_col} 
                    AND fs.question_id = a.question_id
                    AND a.result = 'Incorrect'
                    AND a.timestamp < fs.first_correct_ts
                GROUP BY fs.user_id, fs.question_id
            )
            SELECT user_id, SUM(CASE WHEN prev_failures = 0 THEN 50 ELSE 25 END) as pts
            FROM AttemptCounts
            GROUP BY user_id
        ),
        TotalPoints AS (
            SELECT u.{id_col} as user_id, u.username,
                   COALESCE(q.pts, 0) + COALESCE(c.pts, 0) as total_score
            FROM users u
            LEFT JOIN QuizPoints q ON u.{id_col} = q.user_id
            LEFT JOIN CodingPoints c ON u.{id_col} = c.user_id
        )
        SELECT username, total_score
        FROM TotalPoints
        WHERE total_score > 0
        ORDER BY total_score DESC
        LIMIT %s
    """, (limit,))
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_user_rank_stats(user_id):
    """
    Get a specific user's rank and score for the CURRENT WEEK.
    Returns: (rank, username, score) or None if not ranked.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    id_col = get_user_id_column(cur)
    
    cur.execute(f"""
        WITH QuizPoints AS (
            SELECT user_id, COUNT(*) * 10 as pts
            FROM quiz_answers
            WHERE is_correct = TRUE
              AND created_at >= date_trunc('week', CURRENT_TIMESTAMP)
            GROUP BY user_id
        ),
        CodingPoints AS (
            WITH FirstSuccess AS (
                SELECT {id_col} as user_id, question_id, MIN(timestamp) as first_correct_ts
                FROM attempts
                WHERE result = 'Correct'
                  AND timestamp >= date_trunc('week', CURRENT_TIMESTAMP)
                GROUP BY {id_col}, question_id
            ),
            AttemptCounts AS (
                SELECT 
                    fs.user_id,
                    fs.question_id,
                    COUNT(a.id) as prev_failures
                FROM FirstSuccess fs
                LEFT JOIN attempts a ON fs.user_id = a.{id_col} 
                    AND fs.question_id = a.question_id
                    AND a.result = 'Incorrect'
                    AND a.timestamp < fs.first_correct_ts
                GROUP BY fs.user_id, fs.question_id
            )
            SELECT user_id, SUM(CASE WHEN prev_failures = 0 THEN 50 ELSE 25 END) as pts
            FROM AttemptCounts
            GROUP BY user_id
        ),
        Leaderboard AS (
            SELECT 
                u.{id_col} as user_id,
                u.username,
                COALESCE(q.pts, 0) + COALESCE(c.pts, 0) as total_score,
                RANK() OVER (ORDER BY (COALESCE(q.pts, 0) + COALESCE(c.pts, 0)) DESC) as rank
            FROM users u
            LEFT JOIN QuizPoints q ON u.{id_col} = q.user_id
            LEFT JOIN CodingPoints c ON u.{id_col} = c.user_id
        )
        SELECT rank, username, total_score 
        FROM Leaderboard 
        WHERE user_id = %s
    """, (user_id,))
    
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def save_quiz_answer(poll_id, user_id, user_name, question_id, selected_option_id, is_correct):
    """Save a user's answer to a poll quiz."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            INSERT INTO quiz_answers (poll_id, user_id, user_name, question_id, selected_option_id, is_correct)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (poll_id, user_id) 
            DO UPDATE SET 
                selected_option_id = EXCLUDED.selected_option_id,
                is_correct = EXCLUDED.is_correct;
        """, (poll_id, user_id, user_name, question_id, selected_option_id, is_correct))
        conn.commit()
    except Exception as e:
        print(f"Error saving quiz answer: {e}")
        conn.rollback()
    
    cur.close()
    conn.close()

def save_poll_metadata(poll_id, question_id, correct_option_id, question_text=None, options_json=None):
    """Save poll mapping and content to DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO poll_metadata (poll_id, question_id, correct_option_id, question_text, options_json)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (poll_id) 
        DO UPDATE SET 
            question_text = EXCLUDED.question_text,
            options_json = EXCLUDED.options_json;
    """, (poll_id, question_id, correct_option_id, question_text, options_json))
    conn.commit()
    cur.close()
    conn.close()

def get_all_custom_polls():
    """Get all custom (non-daily) polls from the last 30 days."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT poll_id, question_text, options_json, created_at
        FROM poll_metadata 
        WHERE question_id LIKE 'custom_%%'
        ORDER BY created_at DESC
        LIMIT 20;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

def get_poll_votes_detailed(poll_id):
    """Get summarized votes for a poll."""
    conn = get_db_connection()
    cur = conn.cursor()
    # Join quiz_answers with poll_metadata to get the options
    cur.execute("""
        SELECT qa.user_name, qa.selected_option_id, pm.options_json
        FROM quiz_answers qa
        JOIN poll_metadata pm ON qa.poll_id = pm.poll_id
        WHERE qa.poll_id = %s;
    """, (poll_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    if not rows:
        return []
        
    import json
    results = []
    for r in rows:
        options = json.loads(r['options_json']) if r['options_json'] else []
        opt_text = options[r['selected_option_id']] if 0 <= r['selected_option_id'] < len(options) else f"Option {r['selected_option_id']}"
        results.append({
            "user_name": r['user_name'],
            "option_text": opt_text
        })
    return results

def load_poll_metadata(poll_id):
    """Get metadata for a poll."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT question_id, correct_option_id FROM poll_metadata WHERE poll_id = %s", (poll_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None

# ===================== USER SESSIONS =====================

def save_user_session(chat_id: int, data: Dict) -> None:
    """Save user session to DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # JSON serialization
    json_data = json.dumps(data)
    
    cur.execute("""
        INSERT INTO user_sessions (user_id, data, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE 
        SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at;
    """, (chat_id, json_data, datetime.now()))
    
    conn.commit()
    cur.close()
    conn.close()

def load_user_session(chat_id: int) -> Optional[Dict]:
    """Load user session from DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT data FROM user_sessions WHERE user_id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if row:
        # Extremely defensive: handle dict, tuple, or unexpected string
        if isinstance(row, dict):
            data_str = row.get("data")
        elif isinstance(row, (list, tuple)) and len(row) > 0:
            data_str = row[0]
        else:
            data_str = str(row) # Fallback if somehow it's a string
            
        try:
            return json.loads(data_str)
        except:
            return None
    return None

def clear_user_session(chat_id: int) -> None:
    """Delete user session."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_sessions WHERE user_id = %s", (chat_id,))
    conn.commit()
    cur.close()
    conn.close()

# ===================== GLOBAL CHALLENGE (COMPETITION) =====================

def create_global_challenge(admin_id: int, topic: str, difficulty: str, question_data: Dict) -> int:
    """Save a new global challenge to DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Deactivate any previous challenge
    cur.execute("UPDATE global_challenges SET is_active = FALSE WHERE is_active = TRUE")
    
    # 2. Insert new challenge
    cur.execute("""
        INSERT INTO global_challenges (admin_id, topic, difficulty, question_data, is_active)
        VALUES (%s, %s, %s, %s, TRUE)
        RETURNING id
    """, (admin_id, topic, difficulty, json.dumps(question_data)))
    
    challenge_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    return challenge_id

def get_active_challenge() -> Optional[Dict]:
    """Retrieve the currently active global challenge."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, topic, difficulty, question_data, created_at FROM global_challenges WHERE is_active = TRUE LIMIT 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if row:
        res = dict(row)
        # question_data is JSONB, so psycopg2 already returns it as a dict
        if isinstance(res["question_data"], str):
            res["question_data"] = json.loads(res["question_data"])
        return res
    return None

def get_challenge_results(challenge_id: int) -> List[Dict]:
    """Get list of users who solved a specific challenge."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT a.user_id, u.username, a.timestamp as solved_at
        FROM attempts a
        JOIN users u ON a.user_id = u.user_id
        WHERE a.challenge_id = %s AND a.result = 'Correct'
        ORDER BY a.timestamp ASC
    """, (challenge_id,))
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    results = []
    for r in rows:
        item = dict(r)
        if isinstance(item["solved_at"], datetime):
            item["solved_at"] = item["solved_at"].isoformat()
        results.append(item)
    return results

def get_all_challenges(limit: int = 10) -> List[Dict]:
    """Retrieve history of global challenges."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, topic, difficulty, created_at, is_active FROM global_challenges ORDER BY created_at DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    return [dict(r) for r in rows]

# ===================== FEEDBACK =====================

def save_feedback(user_id: int, username: str, message: str) -> None:
    """Save user feedback to DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        INSERT INTO feedback (user_id, username, message)
        VALUES (%s, %s, %s)
    """, (user_id, username, message))
    
    conn.commit()
    cur.close()
    conn.close()

# ===================== HELPER =====================

def get_user_id_column(cur):
    """Dynamically determine if the column is 'user_id' or 'telegram_id'."""
    try:
        cur.execute("SELECT * FROM users LIMIT 0")
        colnames = [desc[0] for desc in cur.description]
        if 'telegram_id' in colnames:
            return 'telegram_id'
        return 'user_id'
    except:
        return 'user_id' # Default fallback

def ensure_user_exists(user_id, username=None):
    """Ensure the user exists. Only update username if provided."""
    conn = get_db_connection()
    cur = conn.cursor()
    id_col = get_user_id_column(cur)
    
    today = date.today()
    if username:
        cur.execute(f"""
            INSERT INTO users ({id_col}, username, last_activity_date) 
            VALUES (%s, %s, %s) 
            ON CONFLICT ({id_col}) DO UPDATE 
            SET username = EXCLUDED.username, last_activity_date = EXCLUDED.last_activity_date
        """, (user_id, username, today))
    else:
        # Just ensure persistence without wiping legacy name
        cur.execute(f"""
            INSERT INTO users ({id_col}, last_activity_date) 
            VALUES (%s, %s) 
            ON CONFLICT ({id_col}) DO UPDATE
            SET last_activity_date = EXCLUDED.last_activity_date
        """, (user_id, today))
    
    conn.commit()
    cur.close()
    conn.close()

def sync_to_github(file_path):
    """Deprecated. No-op for DB version."""
    pass

# ===================== DIFFICULTY HELPERS =====================

def get_recent_results(user_id: int, limit: int = 3) -> List[str]:
    """
    Get the most recent N evaluation results.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    id_col = get_user_id_column(cur)
    cur.execute(f"""
        SELECT result FROM attempts 
        WHERE {id_col} = %s 
        ORDER BY timestamp DESC 
        LIMIT %s
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    return [row["result"] for row in rows]


def get_weak_topic(user_id: int) -> Optional[str]:
    """
    Find the topic where user has lowest success rate (< 50%).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    id_col = get_user_id_column(cur)
    
    # Query topic stats directly since we maintain them
    cur.execute(f"""
        SELECT topic_name, correct_count, incorrect_count 
        FROM topic_stats 
        WHERE {id_col} = %s
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    weak_topic = None
    lowest_rate = 1.0
    
    for row in rows:
        total = row["correct_count"] + row["incorrect_count"]
        if total >= 3:
            rate = row["correct_count"] / total
            if rate < 0.5 and rate < lowest_rate:
                lowest_rate = rate
                weak_topic = row["topic_name"]
                
    return weak_topic

# ===================== ADMIN HELPERS =====================

def get_all_user_ids() -> List[int]:
    """Get all user IDs for broadcasting."""
    conn = get_db_connection()
    cur = conn.cursor()
    id_col = get_user_id_column(cur)
    
    cur.execute(f"SELECT {id_col} FROM users")
    rows = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return [row[id_col] for row in rows]

def get_system_stats() -> Dict:
    """Get system-wide statistics."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    stats = {}
    
    # 1. Total Users
    cur.execute("SELECT COUNT(*) as count FROM users")
    stats["total_users"] = cur.fetchone()["count"]
    
    # 2. Active Today
    today = date.today()
    cur.execute("SELECT COUNT(*) as count FROM users WHERE last_activity_date = %s", (today,))
    stats["active_today"] = cur.fetchone()["count"]
    
    # 3. Top Topic
    cur.execute("""
        SELECT topic_name, SUM(correct_count + incorrect_count) as total_attempts
        FROM topic_stats
        GROUP BY topic_name
        ORDER BY total_attempts DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    stats["top_topic"] = row["topic_name"] if row else "None"
    
    # 4. Quiz Respondents (Latest Quiz)
    cur.execute("""
        SELECT COUNT(DISTINCT user_id) as count 
        FROM quiz_answers 
        WHERE poll_id = (SELECT poll_id FROM poll_metadata ORDER BY created_at DESC LIMIT 1)
    """)
    row_q = cur.fetchone()
    stats["quiz_participants"] = row_q["count"] if row_q else 0
    
    cur.close()
    conn.close()
    
    return stats

def get_quiz_history(limit=5):
    """Fetch aggregated participation stats for recent quiz broadcasts."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # We group by question_id to merge all poll instances from one broadcast
    # We use MAX(participant_count) logic is wrong, we need to sum distinct users across all poll_ids for that question
    # Correct logic: count distinct user_ids from quiz_answers where poll_id corresponds to this question_id
    cur.execute("""
        SELECT 
            m.question_id, 
            MIN(m.created_at) as broadcast_at,
            (SELECT COUNT(DISTINCT user_id) FROM quiz_answers WHERE poll_id IN (SELECT poll_id FROM poll_metadata WHERE question_id = m.question_id)) as participant_count
        FROM poll_metadata m
        GROUP BY m.question_id
        ORDER BY broadcast_at DESC
        LIMIT %s
    """, (limit,))
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def was_quiz_sent_recently(hour_window=1):
    """Check if any quiz was sent in the last N hours."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Use safer interval arithmetic for Postgres
    cur.execute("""
        SELECT COUNT(*) as count 
        FROM poll_metadata 
        WHERE created_at > (CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour'))
    """, (hour_window,))
    
    row = cur.fetchone()
    count = row["count"] if row else 0
    cur.close()
    conn.close()
    return count > 0
