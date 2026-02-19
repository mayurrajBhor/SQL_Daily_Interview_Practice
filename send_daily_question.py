import json
import random
import uuid
import os
import requests
from dotenv import load_dotenv
from pathlib import Path
from difficulty import decide_difficulty, get_weak_topic
from storage import load_progress, save_last_question
from datetime import date
from llm_helper import generate_question, generate_quiz_question

# ================= PATHS =================
BASE_DIR = Path(__file__).resolve().parent

# ================= ENV ===================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ================= HELPERS =================
def load_json(file):
    with open(BASE_DIR / file, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(file, data):
    with open(BASE_DIR / file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ================= MAIN =================
def broadcast_daily_quiz():
    print("🤖 Starting Daily MCQ Quiz Broadcast...")
    
    # 1. Configuration for Global Quiz (Weighted Rotation)
    topics = load_json("sql_topics.json")
    difficulties = ["beginner", "intermediate", "advanced"]
    weights = [0.2, 0.5, 0.3] # 20% Beginner, 50% Intermediate, 30% Advanced
    challenge_difficulty = random.choices(difficulties, weights=weights, k=1)[0]
    
    # 2. Pick a Random Topic across the selected difficulty
    possible_topics = topics.get(challenge_difficulty, ["SELECT"])
    topic = random.choice(possible_topics)
    
    print(f"🎯 Topic Selected: {topic} ({challenge_difficulty})")

    # 3. Fetch History for Anti-Duplication (GLOBAL Context)
    from storage import get_past_questions
    past_questions = get_past_questions(topic="all", limit=15)
    
    # 4. Generate ONE Global Quiz Question (MCQ)
    quiz_data, retry_count = generate_quiz_question(challenge_difficulty, topic, past_questions=past_questions)

    # 5. Save to Database (Global State)
    # We store the entire JSON in 'question_text' so other parts of the system can parse it if needed
    question_json_str = json.dumps(quiz_data)
    
    new_question = {
        "question_id": str(uuid.uuid4()),
        "date": date.today().isoformat(),
        "difficulty": challenge_difficulty,
        "topic": topic,
        "question": question_json_str, # Store raw JSON
        "retry_count": retry_count
    }
    save_last_question(new_question)
    print("✅ Quiz Question saved to DB.")

    # 5. Broadcast to ALL Users
    from storage import get_all_user_ids
    user_ids = get_all_user_ids()
    
    # --- PRODUCTION MODE: Broadcast to Everyone ---
    # if not CHAT_ID:
    #     print("❌ Error: CHAT_ID not set in .env")
    #     return
    # user_ids = [int(CHAT_ID)]
    # ----------------------------------------------
    
    print(f"📢 Broadcasting to {len(user_ids)} users...")
    
    from storage import save_poll_metadata
    
    success_count = 0
    for user_id in user_ids:
        try:
            poll_id = send_poll_to_user(user_id, quiz_data)
            if poll_id:
                # Save metadata for this poll instance
                save_poll_metadata(
                    poll_id, 
                    new_question["question_id"], 
                    quiz_data["correct_option_id"]
                )
                success_count += 1
        except Exception as e:
            print(f"❌ Failed to send to {user_id}: {e}")

    print(f"🎉 Broadcast complete! Sent to {success_count}/{len(user_ids)} users.")

def send_poll_to_user(chat_id, quiz_data):
    if not BOT_TOKEN:
        print("Missing BOT_TOKEN")
        return None
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll"
    
    payload = {
        "chat_id": chat_id,
        "question": quiz_data["question"],
        "options": json.dumps(quiz_data["options"]),
        "is_anonymous": False,  # Allow seeing who voted (useful for future leaderboards)
        "type": "quiz",
        "correct_option_id": quiz_data["correct_option_id"],
        "explanation": quiz_data["explanation"],
        "explanation_parse_mode": "HTML"
    }
    
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        raise Exception(f"Telegram API Error: {response.text}")
        
    # Extract poll_id from response
    try:
        res_json = response.json()
        if "result" in res_json and "poll" in res_json["result"]:
            return res_json["result"]["poll"]["id"]
    except:
        pass
    return None

if __name__ == "__main__":
    broadcast_daily_quiz()
