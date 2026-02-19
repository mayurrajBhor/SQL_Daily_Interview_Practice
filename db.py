import os
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """Establish a connection to the PostgreSQL database with retries."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set in environment variables.")
    
    max_retries = 3
    retry_delay = 2 # seconds
    
    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10)
            return conn
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                print(f"⚠️ Database connection attempt {attempt+1} failed. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            else:
                print(f"❌ Database connection failed after {max_retries} attempts.")
                raise e

def init_db():
    """Initialize the database schema."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Users Table (Progress Stats)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            current_difficulty TEXT DEFAULT 'beginner',
            total_correct INTEGER DEFAULT 0,
            total_incorrect INTEGER DEFAULT 0,
            streak_days INTEGER DEFAULT 0,
            last_activity_date DATE,
            daily_questions_count INTEGER DEFAULT 0,
            last_reset_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # 2. Topic Stats Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topic_stats (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            topic_name TEXT NOT NULL,
            correct_count INTEGER DEFAULT 0,
            incorrect_count INTEGER DEFAULT 0,
            last_practiced_at DATE,
            UNIQUE(user_id, topic_name)
        );
    """)
    
    # 3. Attempts History Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            question_id TEXT,
            difficulty TEXT,
            topic TEXT,
            question_text TEXT,
            user_answer TEXT,
            result TEXT,
            explanation TEXT,
            retry_count INTEGER DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 4. Daily Questions Table (replaces last_question.json)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_questions (
            date DATE PRIMARY KEY,
            question_id TEXT,
            difficulty TEXT,
            topic TEXT,
            question_text TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 5. User Sessions Table (replaces user_sessions.json)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id BIGINT PRIMARY KEY,
            data TEXT,  -- Stores the session JSON dump
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 6. Feedback Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 7. Quiz Answers Table (NEW - For Polls)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quiz_answers (
            id SERIAL PRIMARY KEY,
            poll_id TEXT,
            user_id BIGINT,
            user_name TEXT,
            question_id TEXT,
            selected_option_id INTEGER,
            is_correct BOOLEAN,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(poll_id, user_id)  -- One vote per poll per user
        );
    """)
    
    # 8. Poll Metadata
    cur.execute("""
        CREATE TABLE IF NOT EXISTS poll_metadata (
            poll_id TEXT PRIMARY KEY,
            question_id TEXT,
            correct_option_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 9. Global Challenges Table (NEW)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS global_challenges (
            id SERIAL PRIMARY KEY,
            admin_id BIGINT,
            topic TEXT,
            difficulty TEXT,
            question_data JSONB,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.commit()
    
    # Run Migrations (Safe to run every time)
    _run_migrations(conn)

    cur.close()
    conn.close()
    print("✅ Database initialized successfully.")

def _run_migrations(conn):
    """Check for missing columns and add them (Auto-Migration)."""
    cur = conn.cursor()
    print("🔄 Checking for schema migrations...")
    
    # 1. Column Renames (Telegram ID -> User ID)
    tables_to_rename = ['users', 'topic_stats', 'attempts']
    for table in tables_to_rename:
        try:
            cur.execute(f"SELECT * FROM {table} LIMIT 0")
            colnames = [desc[0] for desc in cur.description]
            if 'telegram_id' in colnames:
                print(f"  - Renaming telegram_id to user_id in {table}...")
                cur.execute(f"ALTER TABLE {table} RENAME COLUMN telegram_id TO user_id;")
                conn.commit()
        except Exception as e:
            print(f"  ⚠️ Rename error in {table}: {e}")
            conn.rollback()

    # 2. Individual Column Additions
    migrations = [
        ('users', 'current_difficulty', "TEXT DEFAULT 'beginner'"),
        ('users', 'daily_questions_count', "INTEGER DEFAULT 0"),
        ('users', 'last_reset_date', "DATE"),
        ('attempts', 'retry_count', "INTEGER DEFAULT 0"),
        ('daily_questions', 'retry_count', "INTEGER DEFAULT 0"),
        ('poll_metadata', 'question_text', "TEXT"),
        ('poll_metadata', 'options_json', "TEXT"),
        ('topic_stats', 'current_level', "INTEGER DEFAULT 1"),
        ('attempts', 'challenge_id', "INTEGER")
    ]
    
    for table, col, dfn in migrations:
        try:
            cur.execute(f"SELECT * FROM {table} LIMIT 0")
            colnames = [desc[0] for desc in cur.description]
            if col not in colnames:
                print(f"  - Adding column {col} to {table}...")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dfn};")
                conn.commit()
        except Exception as e:
            print(f"  ⚠️ Migration error ({table}.{col}): {e}")
            conn.rollback()

    # 3. Constraint Updates
    try:
        cur.execute("""
            DO $$ 
            BEGIN 
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'daily_questions_pkey') THEN 
                    ALTER TABLE daily_questions DROP CONSTRAINT daily_questions_pkey; 
                END IF; 
            END $$;
        """)
        conn.commit()
    except Exception as e:
        print(f"  ⚠️ Constraint error: {e}")
        conn.rollback()
    
    cur.close()
    print("✅ Schema migrations check completed.")

if __name__ == "__main__":
    try:
        init_db()
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
