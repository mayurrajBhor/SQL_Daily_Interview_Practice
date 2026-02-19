import os
import time
import json
import uuid
import random
import requests
import threading
import pytz
import html
import re
from datetime import date, datetime
from dotenv import load_dotenv
load_dotenv()
from groq import Groq

# Import helpers from our modules
from storage import (
    load_last_question, 
    save_attempt, 
    save_user_session, 
    load_user_session, 
    clear_user_session,
    load_progress,
    update_current_difficulty,
    init_storage,
    get_daily_progress,
    get_users_pending_mission,
    ensure_user_exists,
    get_all_user_ids,
    get_system_stats,
    save_feedback,
    SQL_TOPICS_FILE,
    save_quiz_answer,
    load_poll_metadata,
    get_leaderboard_data,
    get_user_rank_stats,
    get_quiz_history,
    was_quiz_sent_recently,
    create_global_challenge,
    get_active_challenge,
    get_challenge_results,
    get_all_challenges
)
from difficulty import decide_difficulty, get_weak_topic
from llm_helper import evaluate_sql, generate_question, discuss_question
from send_daily_question import broadcast_daily_quiz

# ===================== SETUP =====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN in .env")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADMIN_ID = 837013855  # Hardcoded Admin ID for security

# Global registry for real-time updates
LIVE_ADMIN_MESSAGES = {
    "quiz_history_mid": None
}

# ===================== TELEGRAM API =====================

def get_updates(offset=None, timeout=30):
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(f"{BASE_URL}/getUpdates", params=params)
        return r.json().get("result", [])
    except Exception as e:
        print(f"Polling error: {e}")
        return []

def send_message(chat_id, text, keyboard=None):
    # Safety truncation at 4000 chars to avoid Telegram API errors
    # If too long, strip HTML first to avoid partial tags breaking the parser
    if len(text) > 4000:
        clean_text = re.sub(r'</?.*?>', '', text)
        text = clean_text[:4000] + "\n\n[...] (Message truncated for length)"
        parse_mode = None # Can't use HTML on stripped text
    else:
        parse_mode = "HTML"

    payload = {
        "chat_id": chat_id, 
        "text": text, 
        "parse_mode": parse_mode
    }
    
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
        
    try:
        r = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
        res = r.json()
        if not res.get("ok"):
            description = res.get("description", "")
            # If HTML parsing failed, strip all tags and retry as plain text
            if "can't parse entities" in description:
                print(f"⚠️ HTML parse error, retrying as plain text...")
                payload["parse_mode"] = None
                payload["text"] = re.sub(r'</?.*?>', '', text) # Final deep sweep
                r = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
                res = r.json()
            else:
                print(f"❌ Telegram API Error: {res}")
        else:
            print(f"✅ Message sent to {chat_id}")
        return res
    except Exception as e:
        print(f"❌ Send message error: {e}")
        return {}

def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    
    try:
        r = requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"❌ Edit message error: {e}")
        return {}

def delete_message(chat_id, message_id):
    """Delete a message from Telegram chat."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }
    try:
        r = requests.post(f"{BASE_URL}/deleteMessage", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"❌ Delete message error: {e}")
        return {}

# ===================== UI LAYOUTS =====================
MAIN_MENU = {
    "keyboard": [
        [{"text": "🎯 Practice"}, {"text": "🧩 Topic Wise"}],
        [{"text": "🏆 Leaderboard"}, {"text": "📊 Profile"}],
        [{"text": "🎯 Mission"}, {"text": "📢 Feedback"}]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False
}

CHAT_MENU = {
    "keyboard": [
        [{"text": "🚀 Start New Practice"}, {"text": "📊 Stats"}],
        [{"text": "🏆 Leaderboard"}],
        [{"text": "🛑 Stop Discussion"}]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False
}

ADMIN_MENU = {
    "keyboard": [
        [{"text": "📊 Stats"}, {"text": "📝 Quiz History"}],
        [{"text": "📢 Broadcast"}, {"text": "🏆 Global Challenge"}],
        [{"text": "📈 Challenge Results"}],
        [{"text": "📊 Create Poll"}, {"text": "📊 Poll Results"}],
        [{"text": "🔙 Main Menu"}]
    ],
    "resize_keyboard": True,
    "persistent": True
}



DISCUSSION_MENU = {
    "keyboard": [
        [{"text": "🛑 Stop Discussion"}]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False
}

# ===================== UI KEYBOARDS =====================

DISCUSS_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "💬 Discuss with AI", "callback_data": "start_discussion"}]
    ]
}

# ===================== UI KEYBOARD HELPERS =====================

def get_difficulty_keyboard():
    """Returns the first-step keyboard: Choose Difficulty."""
    keyboard = [
        [
            {"text": "🌱 Beginner", "callback_data": "diff:beginner"},
            {"text": "🚀 Intermediate", "callback_data": "diff:intermediate"}
        ],
        [
            {"text": "🔥 Advanced", "callback_data": "diff:advanced"}
        ],
        [{"text": "🔙 Main Menu", "callback_data": "topic_exit"}]
    ]
    return {"inline_keyboard": keyboard}

def get_topic_keyboard(chat_id, selected_topics=None, filter_diff=None):
    if selected_topics is None:
        selected_topics = []
    
    from curriculum import load_topics_data
    topics_data = load_topics_data()
    keyboard = []
    
    # Safety: Normalize filter_diff
    if filter_diff:
        filter_diff = filter_diff.lower()

    # Show the requested difficulty
    if filter_diff and filter_diff in topics_data:
        topics = topics_data[filter_diff]
        # Header for the selected difficulty
        keyboard.append([{"text": f"--- {filter_diff.upper()} TOPICS ---", "callback_data": "none"}])
        
        for i in range(0, len(topics), 2):
            row = []
            for topic in topics[i:i+2]:
                text = f"✅ {topic}" if topic in selected_topics else topic
                row.append({"text": text, "callback_data": f"t:{topic}"})
            keyboard.append(row)
    else:
        # Fallback: Show all difficulties if filter is missing/invalid
        print(f"WARNING: get_topic_keyboard fallback used for filter_diff='{filter_diff}'")
        for diff, topics in topics_data.items():
            keyboard.append([{"text": f"--- {diff.upper()} TOPICS ---", "callback_data": "none"}])
            for i in range(0, len(topics), 2):
                row = []
                for topic in topics[i:i+2]:
                    text = f"✅ {topic}" if topic in selected_topics else topic
                    row.append({"text": text, "callback_data": f"t:{topic}"})
                keyboard.append(row)
    
    # Control row: Reset & Generate
    count = len(selected_topics)
    gen_text = f"🚀 Generate ({count})" if count > 0 else "🎲 Generate Random"
    
    keyboard.append([
        {"text": "🧹 Reset", "callback_data": "topic_reset"},
        {"text": "🔙 Back", "callback_data": "topic_back"},
        {"text": gen_text, "callback_data": "topic_generate"}
    ])
    
    return {"inline_keyboard": keyboard}

# Instruction for the user when they are expected to answer
SQL_HINT_MSG = "💡 <b>Tip</b>: Submit your answer using the template below for the best experience."
SQL_TEMPLATE = "<pre><code>-- Write your MySQL query here\n\n</code></pre>"

# ===================== LOGIC =====================

def handle_start(chat_id, username="User"):
    # Ensure user row exists
    ensure_user_exists(chat_id, username)
    
    welcome_msg = (
        f"👋 <b>Welcome, {username}!</b>\n\n"
        "I am your SQL Daily Practice Bot!\n"
        "I can help you practice SQL interview questions.\n\n"
        "Tap a button below to start!"
    )
    send_message(chat_id, welcome_msg, keyboard=MAIN_MENU)

from curriculum import get_recommendation, get_mastery_progress, get_available_topics

def handle_practice(chat_id, chosen_topics=None):
    print(f"[{chat_id}] Handling /practice request (Topics: {chosen_topics})...")
    
    # 0. Show Generating Status
    if chosen_topics:
        topics_str = ", ".join(chosen_topics)
        status_text = f"🔍 <i>Generating a question focusing on: <b>{topics_str}</b>...</i>"
    else:
        # Get dynamic recommendation
        rec_topic, rec_diff, rec_reason = get_recommendation(chat_id)
        status_text = f"💡 <b>Recommended:</b> <code>{rec_topic}</code>\n<i>({rec_reason})</i>\n\n🔍 <i>Generating question...</i>"
    
    send_message(chat_id, status_text)

    # 1. Load topics
    print(f"[{chat_id}] Loading topics...")
    try:
        with open(SQL_TOPICS_FILE, "r") as f:
            topics_data = json.load(f)
    except FileNotFoundError:
        print(f"[{chat_id}] Error: Topics file not found.")
        send_message(chat_id, "⚠️ Error: Topics file not found.")
        return

    # 1. Load progress
    progress = load_progress(chat_id)

    # 2 & 3. Difficulty and Topic Selection
    if chosen_topics:
        current_difficulty = progress.get("current_difficulty", "beginner")
        # For simplicity, if manually choosing topics, we might stick to current difficulty 
        # or use logic to possibly increase it. For now, stick to current logic.
        new_difficulty = decide_difficulty(current_difficulty, user_id=chat_id)
        topic = " and ".join(chosen_topics)
        
        # Calculate background topics by taking the "deepest" topic selected
        from curriculum import get_background_topics
        deepest_topic = chosen_topics[0]
        max_background = 0
        
        for t in chosen_topics:
            bg = get_background_topics(t)
            if len(bg) > max_background:
                max_background = len(bg)
                deepest_topic = t
                
        background_topics = get_background_topics(deepest_topic)
    else:
        # Use curriculum recommendation
        # (rec_topic, rec_diff are already fetched above)
        topic = rec_topic
        new_difficulty = rec_diff
        
        # Calculate background topics for recommended topic
        from curriculum import get_background_topics
        background_topics = get_background_topics(topic)
        
        # PERSIST: If the curriculum suggests a new difficulty (e.g. they unlocked Intermediate),
        # we must save it to their progress file so future manual practice starts there too.
        update_current_difficulty(chat_id, new_difficulty)

    # 3.5 Determine Sub-Level (Easy/Medium/Hard)
    sub_level = 1
    topic_stats = progress.get("topic_stats", {})
    
    if chosen_topics:
        # If multiple topics, use the lowest level among them to ensure mastery
        levels = [topic_stats.get(t, {}).get("current_level", 1) for t in chosen_topics]
        min_lvl = min(levels) if levels else 1
        
        # BOOST: 2 topics = Medium+, 3+ topics = Hard
        if len(chosen_topics) == 2:
            sub_level = max(2, min_lvl)
        elif len(chosen_topics) >= 3:
            sub_level = 3
        else:
            sub_level = min_lvl
    else:
        # Use level of the recommended topic
        sub_level = topic_stats.get(topic, {}).get("current_level", 1)

    # 4. Generate Question
    print(f"[{chat_id}] Calling LLM to generate question (Sub-Level: {sub_level})...")
    question_text, retry_count = generate_question(new_difficulty, topic, background_topics=background_topics, sub_level=sub_level)
    print(f"[{chat_id}] Question generated (len={len(question_text)}, retries={retry_count}).")
    
    # 5. Create Session Data
    question_id = str(uuid.uuid4())
    session_data = {
        "question_id": question_id,
        "difficulty": new_difficulty,
        "topic": topic,
        "question": question_text,
        "retry_count": retry_count,
        "date": date.today().isoformat()
    }
    
    # Preserve selected_topics in the session if they exist
    old_session = load_user_session(chat_id) or {}
    if "selected_topics" in old_session:
        session_data["selected_topics"] = old_session["selected_topics"]
        
    save_user_session(chat_id, session_data)
    
    # 7. Send Question
    print(f"[{chat_id}] Sending question to user...")
    
    level_map = {1: "Easy", 2: "Medium", 3: "Hard"}
    sub_level_name = level_map.get(sub_level, "Easy")
    header = f"📖 <b>Topic:</b> {topic} (<i>{sub_level_name}</i>)\n\n"
    
    final_output = header + question_text
    if retry_count > 0:
        final_output = f"🛡️ <i>Note: Question formatting auto-corrected by system ({retry_count} retries).</i>\n\n{final_output}"
    
    send_message(chat_id, final_output, keyboard=MAIN_MENU)
    
    # 8. Send Instructions
    send_message(chat_id, SQL_HINT_MSG, keyboard=MAIN_MENU)
    send_message(chat_id, SQL_TEMPLATE, keyboard=DISCUSS_KEYBOARD)

def handle_topic_selection_menu(chat_id):
    msg = "🧩 <b>Topic Wise Practice</b>\n\nChoose a difficulty level to explore topics:"
    send_message(chat_id, msg, keyboard=get_difficulty_keyboard())

def handle_callback(chat_id, callback_data, callback_id, message_id):
    session = load_user_session(chat_id) or {}
    selected = session.get("selected_topics", [])

    if callback_data == "none":
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": "⚠️ Level Locked: Solve more questions to unlock!",
            "show_alert": True
        })
        return

    if callback_data.startswith("diff:"):
        # 1. Step: Set Filter and show topics
        diff = callback_data.split(":")[1]
        session["filter_difficulty"] = diff
        save_user_session(chat_id, session)
        
        msg = f"🧩 <b>Choose {diff.capitalize()} Topics</b>\n\nSelect one or more topics below. You can go back to pick other levels too!"
        edit_message(chat_id, message_id, msg, keyboard=get_topic_keyboard(chat_id, selected, diff))
        return

    if callback_data == "topic_back":
        # 2. Step: Return to Difficulty Selection
        session.pop("filter_difficulty", None)
        save_user_session(chat_id, session)
        msg = "🧩 <b>Topic Wise Practice</b>\n\nChoose a difficulty level to explore topics:"
        edit_message(chat_id, message_id, msg, keyboard=get_difficulty_keyboard())
        return

    if callback_data == "topic_exit":
        delete_message(chat_id, message_id)
        return

    if callback_data.startswith("t:"):
        topic = callback_data.replace("t:", "")
        alert_text = ""
        if topic in selected:
            selected.remove(topic)
            alert_text = f"❌ Removed: {topic}"
        else:
            selected.append(topic)
            alert_text = f"✅ Added: {topic}"
        
        # Answer with toast
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": alert_text
        })
        
        # Save updated selection
        session["selected_topics"] = selected
        save_user_session(chat_id, session)
        
        # Update current filtered view
        filter_diff = session.get("filter_difficulty")
        edit_message(
            chat_id, 
            message_id, 
            f"🧩 <b>Choose {filter_diff.capitalize() if filter_diff else 'SQL'} Topics</b>\n\nSelect one or more topics below.",
            keyboard=get_topic_keyboard(chat_id, selected, filter_diff)
        )
        
    elif callback_data == "scoring_info":
        info_text = (
            "🏆 <b>How to Earn Points</b>\n\n"
            "📝 <b>Quizzes</b>: +10 pts per correct answer.\n"
            "💻 <b>SQL Practice</b>:\n"
            "• <b>First Try</b>: +50 pts 🔥\n"
            "• <b>Successful Retry</b>: +25 pts\n\n"
            "<i>Only the first solve per question earns points. Keep practicing and climb the ranks!</i>"
        )
        send_message(chat_id, info_text)
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id
        })
        
    elif callback_data == "topic_reset":
        session["selected_topics"] = []
        save_user_session(chat_id, session)
        
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": "🧹 Selection cleared"
        })
        
        filter_diff = session.get("filter_difficulty")
        if filter_diff:
            edit_message(
                chat_id, 
                message_id, 
                f"🧩 <b>Choose {filter_diff.capitalize()} Topics</b>\n\nSelect one or more topics below.",
                keyboard=get_topic_keyboard(chat_id, [], filter_diff)
            )
        else:
            handle_topic_selection_menu(chat_id)
        
    elif callback_data == "topic_generate":
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": "🚀 Generating your practice question..."
        })
        top_list = session.get("selected_topics", [])
        handle_practice(chat_id, chosen_topics=top_list)
        
    elif callback_data.startswith("view_poll_results:"):
        poll_id = callback_data.split(":")[1]
        render_poll_results(chat_id, poll_id)
        
    elif callback_data.startswith("view_challenge_res:"):
        c_id = int(callback_data.split(":")[1])
        render_challenge_results(chat_id, c_id)

    # --- ADMIN CHALLENGE WIZARD CALLBACKS ---
    elif callback_data.startswith("cd:"):
        diff = callback_data.split(":")[1]
        print(f"DEBUG: Admin picked challenge diff: {diff}")
        session["challenge_diff"] = diff
        session["challenge_topics"] = [] # Initialize multi-topic list
        save_user_session(chat_id, session)

        # Answer callback to stop loading spinner
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})

        from curriculum import load_topics_data
        topics_data = load_topics_data()
        category_topics = topics_data.get(diff, [])
        print(f"DEBUG: Found {len(category_topics)} topics for {diff}")

        kb_rows = []
        for i in range(0, len(category_topics), 2):
            row = []
            for t in category_topics[i:i+2]:
                # Add checkmark if selected
                is_sel = t in session.get("challenge_topics", [])
                label = f"✅ {t}" if is_sel else t
                row.append({"text": label, "callback_data": f"ct:{t}"})
            kb_rows.append(row)
        
        # Add "Generate" button if at least one topic is selected
        if session.get("challenge_topics"):
            kb_rows.append([{"text": "🚀 Generate Challenge", "callback_data": "cg:"}])
        
        msg = f"🏆 <b>Global Challenge: {diff.upper()}</b>\n\nStep 2/3: Select <b>one or more Topics</b> below."
        res = edit_message(chat_id, message_id, msg, keyboard={"inline_keyboard": kb_rows})
        print(f"DEBUG: edit_message result: {res}")

    elif callback_data.startswith("ct:"):
        topic = callback_data.split(":")[1]
        diff = session.get("challenge_diff")
        
        # Toggle topic
        topics = session.get("challenge_topics", [])
        if topic in topics:
            topics.remove(topic)
        else:
            topics.append(topic)
        session["challenge_topics"] = topics
        save_user_session(chat_id, session)

        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})

        # Re-render topics menu
        from curriculum import load_topics_data
        topics_data = load_topics_data()
        category_topics = topics_data.get(diff, [])
        
        kb_rows = []
        for i in range(0, len(category_topics), 2):
            row = []
            for t in category_topics[i:i+2]:
                is_sel = t in topics
                label = f"✅ {t}" if is_sel else t
                row.append({"text": label, "callback_data": f"ct:{t}"})
            kb_rows.append(row)
            
        if topics:
            kb_rows.append([{"text": "🚀 Generate Challenge", "callback_data": "cg:"}])
            
        msg = f"🏆 <b>Global Challenge: {diff.upper()}</b>\n\nSelected: <code>{', '.join(topics) if topics else 'None'}</code>\n\nStep 2/3: Select <b>one or more Topics</b> below."
        edit_message(chat_id, message_id, msg, keyboard={"inline_keyboard": kb_rows})

    elif callback_data == "cg:":
        topics = session.get("challenge_topics", [])
        diff = session.get("challenge_diff")
        
        if not topics:
            requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": "Please select at least one topic!", "show_alert": True})
            return

        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})
        
        # Combined topic name for LLM
        combined_topic = " and ".join(topics)
        edit_message(chat_id, message_id, f"🔍 <b>Generating Challenge Question...</b>\nTopics: <code>{combined_topic}</code>\nLevel: <code>{diff}</code>")

        # Generate the question
        question_text, retry_count = generate_question(diff, combined_topic)
        if not question_text:
            edit_message(chat_id, message_id, "❌ Generation failed. Try again via 🏆 Global Challenge.")
            return

        question_data = {
            "question": question_text,
            "retry_count": retry_count,
            "topic": combined_topic,
            "difficulty": diff
        }
        session["challenge_q"] = question_data
        save_user_session(chat_id, session)

        preview = f"🏆 <b>CHALLENGE PREVIEW</b>\n\nTopics: {combined_topic}\nLevel: {diff}\n\n{question_data['question']}"
        preview = preview[:3000]
        
        kb = {"inline_keyboard": [[{"text": "📋 Review & Broadcast", "callback_data": "cc_pre:"}]]}
        edit_message(chat_id, message_id, preview, keyboard=kb)

    elif callback_data == "reveal_answer":
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})
        sql = session.get("last_correct_sql")
        if not sql:
            send_message(chat_id, "⚠️ <b>Answer expired.</b> Please submit your query again to see the solution.")
        else:
            msg = f"🔑 <b>Correct SQL Answer:</b>\n\n<pre><code>{sql}</code></pre>"
            send_message(chat_id, msg)

    elif callback_data == "cc_pre:":
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})
        q_data = session.get("challenge_q", {})
        topic = q_data.get("topic", "Unknown Topic")
        diff = q_data.get("difficulty", "intermediate")
        
        msg = (
            f"⚠️ <b>CONFIRM GLOBAL BROADCAST</b> ⚠️\n\n"
            f"You are about to launch a competition for ALL users.\n\n"
            f"📍 <b>Topics</b>: {topic}\n"
            f"🔥 <b>Level</b>: {diff.upper()}\n\n"
            f"<b>Are you sure you want to send this now?</b>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "🔥 Yes, Broadcast Now", "callback_data": "cc:"}],
                [{"text": "🛑 Cancel", "callback_data": "topic_exit"}]
            ]
        }
        edit_message(chat_id, message_id, msg, keyboard=kb)

    elif callback_data == "cc:":
        print("DEBUG: Admin confirmed challenge. Starting broadcast...")
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})
        
        q_data = session.get("challenge_q")
        if not q_data:
            edit_message(chat_id, message_id, "❌ Session expired or question not generated. Please restart.")
            return

        topic = q_data.get("topic")
        diff = q_data.get("difficulty")

        # 1. Create Challenge in DB
        c_id = create_global_challenge(chat_id, topic, diff, q_data)

        # 2. Get All Users
        users = get_all_user_ids()
        count = 0
        
        challenge_msg = (
            f"🏆 <b>GLOBAL CHALLENGE STARTED!</b> 🏆\n\n"
            f"Admin has launched a new competition for everyone!\n\n"
            f"📍 <b>Topics</b>: {topic}\n"
            f"🔥 <b>Level</b>: {diff.capitalize()}\n\n"
            f"<i>Tap below to participate!</i>"
        )
        
        kb = {
            "inline_keyboard": [[{"text": "🚀 Solve Challenge", "callback_data": f"start_challenge:{c_id}"}]]
        }

        for uid in users:
            try:
                res = send_message(uid, challenge_msg, keyboard=kb)
                if res.get("ok"): count += 1
            except: pass

        edit_message(chat_id, message_id, f"✅ <b>Challenge Live!</b>\nBroadcast sent to {count} users.")
        session.pop("challenge_diff", None)
        session.pop("challenge_topic", None)
        session.pop("challenge_q", None)
        save_user_session(chat_id, session)
        
    elif callback_data == "start_discussion":
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": "💬 Opening AI discussion..."
        })

        if not session or "question" not in session:
            send_message(chat_id, "❌ No active question to discuss.")
            return
            
        # Switch to chat mode
        session["mode"] = "DISCUSSION"
        session["chat_history"] = []
        save_user_session(chat_id, session)
        
        send_message(
            chat_id, 
            "💬 <b>Discussion Mode Active!</b>\n\nAsk me anything about this question or SQL topic. I'm here to help!\n\n<i>Tap 🛑 Stop Discussion to return to the question.</i>",
            keyboard=DISCUSSION_MENU
        )

    elif callback_data.startswith("start_challenge:"):
        c_id = int(callback_data.split(":")[1])
        # Force a refresh of active challenge from DB
        challenge = get_active_challenge()
        
        if not challenge or challenge["id"] != c_id:
            requests.post(f"{BASE_URL}/answerCallbackQuery", json={
                "callback_query_id": callback_id,
                "text": "❌ This challenge is no longer active.",
                "show_alert": True
            })
            return

        # Load into session
        session["question"] = challenge["question_data"]["question"]
        session["difficulty"] = challenge["difficulty"]
        session["topic"] = challenge["topic"]
        session["question_id"] = challenge["question_data"].get("question_id")
        session["challenge_id"] = c_id
        session["try_count"] = 0
        save_user_session(chat_id, session)

        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": "🚀 Challenge Started!"
        })

        msg = (
            f"🏆 <b>GLOBAL CHALLENGE</b>\n"
            f"Topic: <code>{challenge['topic']}</code>\n"
            f"Level: <code>{challenge['difficulty'].capitalize()}</code>\n\n"
            f"{challenge['question_data']['question']}"
        )
        send_message(chat_id, msg)
        send_message(chat_id, SQL_HINT_MSG, keyboard=MAIN_MENU)
        send_message(chat_id, SQL_TEMPLATE, keyboard=DISCUSS_KEYBOARD)
        return

        


def handle_profile(chat_id):
    print(f"[{chat_id}] Loading profile...")
    progress = load_progress(chat_id)
    print(f"[{chat_id}] Profile loaded.")
    
    total = progress.get("total_correct", 0) + progress.get("total_incorrect", 0)
    correct = progress.get("total_correct", 0)
    streak = progress.get("streak_days", 0)
    
    # Calculate accuracy
    accuracy = (correct / total * 100) if total > 0 else 0
    
    # Topic breakdown
    topic_stats = progress.get("topic_stats", {})
    topic_msg = ""
    
    # Sort topics by most attempted
    sorted_topics = sorted(
        topic_stats.items(), 
        key=lambda x: x[1]['correct'] + x[1]['incorrect'], 
        reverse=True
    )
    
    for topic, stats in sorted_topics:
        t_correct = stats.get("correct", 0)
        t_total = t_correct + stats.get("incorrect", 0)
        t_level = stats.get("current_level", 1)
        level_map = {1: "Easy", 2: "Medium", 3: "Hard"}
        level_str = level_map.get(t_level, "Easy")
        topic_msg += f"• <b>{topic}</b>: {t_correct}/{t_total} ✅ (<i>{level_str}</i>)\n"

    msg = (
        f"📊 <b>Your SQL Profile</b>\n\n"
        f"🔥 <b>Streak</b>: {streak} days\n"
        f"🎯 <b>Accuracy</b>: {accuracy:.1f}%\n"
        f"📝 <b>Total Solved</b>: {total}\n"
        f"✅ <b>Correct</b>: {correct}\n\n"
    )

    # Mastery Progress Section
    mastery_data = get_mastery_progress(progress)
    if mastery_data["status"] == "maxed":
         msg += f"🏆 <b>Mastery</b>: {mastery_data['message']}\n\n"
    else:
         curr_lvl = mastery_data['current_level'].capitalize()
         next_lvl = mastery_data['next_level'].capitalize()
         c_done = mastery_data['correct']
         c_targ = mastery_data['correct_target']
         a_curr = mastery_data['accuracy'] * 100
         a_targ = mastery_data['accuracy_target'] * 100
         
         # Logic for checkmarks
         c_check = "✅" if c_done >= c_targ else "⏳"
         a_check = "✅" if a_curr >= a_targ else "⏳"

         msg += (
             f"🚀 <b>Next Level Progress: {next_lvl}</b>\n"
             f"• {curr_lvl} Solved: {c_done}/{c_targ} {c_check}\n"
             f"• {curr_lvl} Accuracy: {a_curr:.0f}% / {a_targ:.0f}% {a_check}\n\n"
         )

    msg += (
        f"📂 <b>Topic Performance</b>:\n"
        f"{topic_msg if topic_msg else 'No topics practiced yet.'}"
    )
    
    send_message(chat_id, msg, keyboard=MAIN_MENU)

def handle_mission(chat_id):
    """Handle the daily mission button."""
    progress = get_daily_progress(chat_id)
    
    done = progress["completed"]
    target = progress["target"]
    is_complete = progress["is_complete"]
    
    # Progress bar
    bar_len = target
    filled = min(done, bar_len)
    bar = "🟩" * filled + "⬜" * (bar_len - filled)
    
    msg = (
        f"🎯 <b>Daily Mission</b>\n\n"
        f"<b>Goal</b>: Solve {target} questions today!\n"
        f"<b>Progress</b>: {done}/{target}\n\n"
        f"{bar}\n\n"
    )
    
    if is_complete:
        msg += "🎉 <b>Mission Complete!</b> You're on fire! 🔥\nKeep practicing to maintain your streak!"
    else:
        remaining = target - done
        msg += f"💪 <b>Keep going!</b> Only {remaining} more to go."
    
    send_message(chat_id, msg, keyboard=MAIN_MENU)

def handle_feedback_command(chat_id, session):
    """Start feedback flow."""
    send_message(chat_id, "📝 <b>Please type your feedback below:</b>\n(Or tap '🛑 Stop Discussion' to cancel)", keyboard=CHAT_MENU)
    session["state"] = "awaiting_feedback"
    save_user_session(chat_id, session)

def handle_feedback_message(chat_id, username, text, session):
    """Process feedback text."""
    save_feedback(chat_id, username, text)
    send_message(chat_id, "✅ <b>Feedback received!</b>\nThank you for helping us improve.", keyboard=MAIN_MENU)
    session.pop("state", None)
    save_user_session(chat_id, session)



def handle_admin_command(chat_id, text, session):
    """Handle Admin Dashboard commands."""
    if chat_id != ADMIN_ID:
        # Ignore unauthorized users
        return

    if text == "/admin":
        send_message(chat_id, "👮‍♂️ <b>Admin Dashboard</b>\n\nSelect an option below:", keyboard=ADMIN_MENU)
        session["mode"] = "ADMIN"
        save_user_session(chat_id, session)
        return

    if text == "🔙 Main Menu":
        session.pop("mode", None)
        save_user_session(chat_id, session)
        handle_start(chat_id)
        return

    if text == "/leaderboard" or text == "🏆 Leaderboard":
        handle_leaderboard(chat_id)
        return

    if text == "📊 Stats":
        stats = get_system_stats()
        msg = (
            "📊 <b>System Statistics</b>\n\n"
            f"👤 <b>Total Users</b>: {stats['total_users']}\n"
            f"🟢 <b>Active Today</b>: {stats['active_today']}\n"
            f"🔥 <b>Top Topic</b>: {stats['top_topic']}\n"
            f"📝 <b>Quiz Participants</b>: {stats.get('quiz_participants', 0)}\n"
        )
        send_message(chat_id, msg)
        return

    if text == "📝 Quiz History":
        history = get_quiz_history(limit=5)
        if not history:
            send_message(chat_id, "📝 <b>Quiz History is Empty.</b>")
            return
            
        msg = "📝 <b>Last 5 Quizzes Participation</b> (Real-time ⚡️)\n\n"
        for row in history:
            q_id = row['question_id']
            count = row['participant_count']
            date_str = row['broadcast_at'].strftime("%d %b, %H:%M")
            msg += f"• <b>Q:{q_id}</b> ({date_str}): 👥 {count} users\n"
            
        res = send_message(chat_id, msg)
        # Store message ID for live updates
        if res.get("ok"):
            LIVE_ADMIN_MESSAGES["quiz_history_mid"] = res["result"]["message_id"]
        return

    if text == "📢 Broadcast":
        send_message(chat_id, "📝 <b>Broadcast Mode</b>\n\nType your message below. It will be sent to ALL users.\n\nType 'CANCEL' to abort.", keyboard={"keyboard": [[{"text": "🛑 Cancel"}]], "resize_keyboard": True})
        session["state"] = "awaiting_broadcast"
        save_user_session(chat_id, session)
        return

    if text == "📊 Create Poll":
        send_message(chat_id, "📊 <b>Create Poll Wizard</b>\n\nStep 1/2: Enter the <b>Question Text</b>.\n\nType 'CANCEL' to abort.", keyboard={"keyboard": [[{"text": "🛑 Cancel"}]], "resize_keyboard": True})
        session["state"] = "awaiting_poll_question"
        save_user_session(chat_id, session)
        return

    if text == "📊 Poll Results":
        show_poll_results_list(chat_id)
        return

    if text == "🏆 Global Challenge":
        msg = "🏆 <b>Global Challenge Setup</b>\n\nStep 1/3: Select <b>Difficulty</b> for the competition."
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🌱 Beginner", "callback_data": "cd:beginner"},
                    {"text": "🚀 Intermediate", "callback_data": "cd:intermediate"}
                ],
                [
                    {"text": "🔥 Advanced", "callback_data": "cd:advanced"}
                ]
            ]
        }
        send_message(chat_id, msg, keyboard=kb)
        session.pop("state", None) # Clear any stuck states
        save_user_session(chat_id, session)
        return

    if text == "📈 Challenge Results":
        show_challenge_results_list(chat_id)
        return



def refresh_live_quiz_history():
    """Update the live Quiz History message for admin if it exists."""
    mid = LIVE_ADMIN_MESSAGES.get("quiz_history_mid")
    if not mid:
        return
        
    history = get_quiz_history(limit=5)
    if not history:
        return
        
    msg = "📝 <b>Last 5 Quizzes Participation</b> (Real-time ⚡️)\n\n"
    for row in history:
        q_id = row['question_id']
        count = row['participant_count']
        date_str = row['broadcast_at'].strftime("%d %b, %H:%M")
        msg += f"• <b>Q:{q_id}</b> ({date_str}): 👥 {count} users\n"
        
    edit_message(ADMIN_ID, mid, msg)

def handle_broadcast_message(chat_id, text, session):
    """Execute broadcast with confirmation."""
    if chat_id != ADMIN_ID:
        return

    if text == "🛑 Cancel" or text == "CANCEL":
        send_message(chat_id, "🚫 Operation cancelled.", keyboard=ADMIN_MENU)
        session.pop("state", None)
        session.pop("broadcast_text", None)
        session.pop("poll_question", None)
        save_user_session(chat_id, session)
        return

    state = session.get("state")

    # --- BROADCAST FLOW ---
    if state == "awaiting_broadcast":
        # Save text and ask for confirmation
        session["broadcast_text"] = text
        session["state"] = "confirm_broadcast"
        save_user_session(chat_id, session)
        
        preview = text[:200] + "..." if len(text) > 200 else text
        msg = f"📢 <b>Broadcast Preview:</b>\n\n{preview}\n\n⚠️ <b>Send to ALL users?</b>"
        
        kb = {"keyboard": [[{"text": "✅ Send Broadcast"}, {"text": "🛑 Cancel"}]], "resize_keyboard": True}
        send_message(chat_id, msg, keyboard=kb)
        return

    elif state == "confirm_broadcast":
        if text == "✅ Send Broadcast":
            final_text = session.get("broadcast_text", "")
            user_ids = get_all_user_ids()
            count = 0
            
            send_message(chat_id, f"🚀 Sending to {len(user_ids)} users...")
            
            for uid in user_ids:
                if uid == ADMIN_ID: continue
                try:
                    send_message(uid, f"📢 <b>Announcement:</b>\n\n{final_text}")
                    count += 1
                except:
                    pass
            
            send_message(chat_id, f"✅ Broadcast sent to {count} users.", keyboard=ADMIN_MENU)
            session.pop("state", None)
            session.pop("broadcast_text", None)
            save_user_session(chat_id, session)
        else:
            # Re-prompt if they type something else, or handle as Cancel if they type Cancel (handled above)
            send_message(chat_id, "⚠️ Please tap '✅ Send Broadcast' or '🛑 Cancel'.")
        return

    # --- POLL CREATION FLOW ---
    elif state == "awaiting_poll_question":
        session["poll_question"] = text
        session["state"] = "awaiting_poll_options"
        save_user_session(chat_id, session)
        
        msg = (
            "📊 <b>Step 2/2: Enter Options</b>\n\n"
            "Separate options with commas. (e.g., 'Yes, No, Maybe')\n"
            "Minimum 2 options.\n"
            "Type 'CANCEL' to abort."
        )
        send_message(chat_id, msg)
        return

    elif state == "awaiting_poll_options":
        options = [opt.strip() for opt in text.split(",") if opt.strip()]
        
        if len(options) < 2:
            send_message(chat_id, "❌ <b>Error:</b> Please provide at least 2 options separated by commas.")
            return
            
        if len(options) > 10:
             send_message(chat_id, "❌ <b>Error:</b> Maximum 10 options allowed.")
             return

        # Confirm Poll
        session["poll_options"] = options
        session["state"] = "confirm_poll"
        save_user_session(chat_id, session)
        
        question = session.get("poll_question", "")
        opt_list = "\n".join([f"• {o}" for o in options])
        
        msg = (
            f"📊 <b>Poll Preview:</b>\n\n"
            f"<b>Q:</b> {question}\n"
            f"<b>Options:</b>\n{opt_list}\n\n"
            f"⚠️ <b>Send to ALL users?</b>"
        )
        
        kb = {"keyboard": [[{"text": "✅ Send Poll"}, {"text": "🛑 Cancel"}]], "resize_keyboard": True}
        send_message(chat_id, msg, keyboard=kb)
        return

    elif state == "confirm_poll":
        if text == "✅ Send Poll":
            question = session.get("poll_question")
            options = session.get("poll_options")
            
            user_ids = get_all_user_ids()
            count = 0
            
            send_message(chat_id, f"🚀 Sending Poll to {len(user_ids)} users...")
            
            # Send Poll Loop
            from send_daily_question import send_poll_to_user # Reuse existing helper logic or use direct API
             # But send_poll_to_user expects complex quiz_data structure (dict with explanation etc).
             # We should use a simpler direct call here or mock the structure.
             # Let's use a direct call for custom polls as they have no correct answer/explanation usually.
            
            for uid in user_ids:
                if uid == ADMIN_ID: continue
                try:
                    # Send NON-ANONYMOUS poll
                    payload = {
                        "chat_id": uid,
                        "question": question,
                        "options": json.dumps(options),
                        "is_anonymous": False,
                        "type": "regular", # Regular poll, not quiz
                        "allows_multiple_answers": False
                    }
                    r = requests.post(f"{BASE_URL}/sendPoll", json=payload)
                    res = r.json()
                    
                    if res.get("ok"):
                        poll_id = res["result"]["poll"]["id"]
                        # Save metadata so we can track the vote later
                        # For regular polls, correct_option_id is -1 (none)
                        from storage import save_poll_metadata
                        save_poll_metadata(
                            poll_id, 
                            question_id=f"custom_{int(time.time())}", 
                            correct_option_id=-1,
                            question_text=question,
                            options_json=json.dumps(options)
                        )
                        count += 1

                except Exception as e:
                    print(f"Poll send error for {uid}: {e}")
                    pass

            send_message(chat_id, f"✅ Poll sent to {count} users.", keyboard=ADMIN_MENU)
            session.pop("state", None)
            session.pop("poll_question", None)
            session.pop("poll_options", None)
            save_user_session(chat_id, session)
        else:
            send_message(chat_id, "⚠️ Please tap '✅ Send Poll' or '🛑 Cancel'.")
        return



def handle_leaderboard(chat_id):
    """Show Top 5 Global + User Rank."""
    # 1. Get Top 5
    top_data = get_leaderboard_data(limit=5)
    
    if not top_data:
        send_message(chat_id, "🏆 <b>Leaderboard is Empty</b>\n\nNo one has won a quiz yet! Be the first!")
        return

    msg = "🏆 <b>Weekly Leaderboard</b>\n"
    msg += "<i>(Mon - Sun reset cycle)</i>\n\n"
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    
    user_in_top = False
    
    # 2. Render Top 5
    for i, row in enumerate(top_data):
        rank_display = medals[i]
        username = row['username'] or f"User {random.randint(1000,9999)}"
        score = row['total_score']
        
        # Highlight if it's the current user
        # We need to check both username and ID since row lacks ID
        # But for display, just username bolding is enough context usually
        
        msg += f"{rank_display} <b>{username}</b>: {score} pts\n"

    # 3. Personal Rank (if User not in Top 5)
    # We fetch the exact rank which uses user_id
    my_rank_data = get_user_rank_stats(chat_id)
    
    if my_rank_data:
        rank = my_rank_data['rank']
        my_name = my_rank_data['username']
        my_score = my_rank_data['total_score']
        
        # Check if rank > 5 (since we showed top 5)
        if rank > 5:
            msg += "\n...\n"
            msg += f"<b>{rank}. You ({my_name})</b>: {my_score} pts\n"
    else:
        # User hasn't played yet
        msg += "\n...\n"
        msg += f"None. <b>You</b>: 0 pts (Play a quiz!)\n"
        
    msg += "\n<i>Points = Quizzes + Coding Practice</i>"
    
    scoring_kb = {
        "inline_keyboard": [
            [{"text": "ℹ️ Scoring Info", "callback_data": "scoring_info"}]
        ]
    }
    send_message(chat_id, msg, keyboard=scoring_kb)

def show_poll_results_list(chat_id):
    """Show a list of historical custom polls."""
    from storage import get_all_custom_polls
    polls = get_all_custom_polls()
    
    if not polls:
        send_message(chat_id, "📭 No custom polls found in the database.")
        return
        
    msg = "📊 <b>Poll Results</b>\n\nSelect a poll below to see the vote breakdown:"
    buttons = []
    
    for p in polls:
        q_text = p.get('question_text') or "Untitled Poll"
        # Truncate if too long for button
        btn_text = (q_text[:30] + '...') if len(q_text) > 30 else q_text
        buttons.append([{"text": f"📍 {btn_text}", "callback_data": f"view_poll_results:{p['poll_id']}"}])
        
    kb = {"inline_keyboard": buttons}
    send_message(chat_id, msg, keyboard=kb)

def render_poll_results(chat_id, poll_id):
    """Render detailed results for a specific poll."""
    from storage import get_poll_votes_detailed
    votes = get_poll_votes_detailed(poll_id)
    
    if not votes:
        send_message(chat_id, "📭 <b>This poll has no votes yet.</b>\n\nEnsure you've sent it to users and they have actually voted.")
        return
        
    msg = f"<b>📊 Poll Results Breakdown</b>\n"
    msg += f"<code>ID: {poll_id}</code>\n\n"
    
    # Simple summary table
    option_counts = {}
    for v in votes:
        opt = v['option_text']
        option_counts[opt] = option_counts.get(opt, 0) + 1
        
    msg += "📈 <b>Summary:</b>\n"
    for opt, count in option_counts.items():
        # Escape HTML for options as they often contain SQL
        safe_opt = html.escape(opt)
        msg += f"• {safe_opt}: <b>{count} votes</b>\n"
        
    msg += "\n👤 <b>Individual Votes:</b>\n"
    for v in votes:
        # Escape HTML for usernames and options
        safe_name = html.escape(v['user_name'])
        safe_opt = html.escape(v['option_text'])
        msg += f"• <b>{safe_name}</b>: <i>{safe_opt}</i>\n"
        
    send_message(chat_id, msg)

def show_challenge_results_list(chat_id):
    """Show a list of historical global challenges for admin to pick."""
    from storage import get_all_challenges
    challenges = get_all_challenges(limit=20)
    
    if not challenges:
        send_message(chat_id, "📭 No global challenges found.")
        return
        
    msg = "🏆 <b>Challenge Results</b>\n\nSelect a challenge to view participation stats:"
    buttons = []
    
    for c in challenges:
        topic = c.get('topic', 'Unknown Topic')
        diff = c.get('difficulty', '')
        active_tag = "🟢 " if c.get('is_active') else "⚪️ "
        btn_text = f"{active_tag}{topic} ({diff})"
        # Truncate if too long for button
        btn_text = (btn_text[:30] + '...') if len(btn_text) > 30 else btn_text
        buttons.append([{"text": btn_text, "callback_data": f"view_challenge_res:{c['id']}"}])
        
    kb = {"inline_keyboard": buttons}
    send_message(chat_id, msg, keyboard=kb)

def render_challenge_results(chat_id, challenge_id):
    """Render detailed list of winners for a specific challenge."""
    from storage import get_challenge_results
    results = get_challenge_results(challenge_id)
    
    if not results:
        send_message(chat_id, f"📭 <b>No one has solved Challenge #{challenge_id} yet.</b>")
        return
        
    msg = f"🏆 <b>Challenge Results (#{challenge_id})</b>\n\n"
    msg += "<b>Winners (by time):</b>\n"
    
    for i, res in enumerate(results):
        name = html.escape(res['username'] or "User")
        time_str = res['solved_at']
        try:
            dt = datetime.fromisoformat(time_str)
            pretty_time = dt.strftime("%H:%M:%S (%d %b)")
        except:
            pretty_time = time_str
            
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "•"
        msg += f"{medal} <b>{name}</b> - {pretty_time}\n"
        
    send_message(chat_id, msg)

def handle_answer(chat_id, user_answer):

    # 1. Check for active personal session
    session = load_user_session(chat_id)
    
    if session and "question" in session:
        # User is answering a /practice question
        target_question = session
        is_practice = True
    else:
        # User is answering the global Daily Question
        target_question = load_last_question()
        is_practice = False
        
        # Basic check to ensure there IS a daily question
        if not target_question or "question" not in target_question:
            send_message(chat_id, "⚠️ No active question found. Use /practice to get one.")
            return

    # 2. Notify processing
    send_message(chat_id, "⏳ <b>Evaluating your answer...</b>")

    # 3. Calculate Background Topics for evaluation
    from curriculum import get_background_topics
    # If topic contains " and ", take the first one for background calculation
    base_topic = target_question.get("topic", "SELECT").split(" and ")[0]
    background_topics = get_background_topics(base_topic)

    # 4. Evaluate
    feedback_full, result, explanation, correct_sql, retry_count_llm = evaluate_sql(
        target_question["question"],
        user_answer,
        background_topics=background_topics
    )

    # 4. Save Attempt
    attempt_id = save_attempt(chat_id, {
        "question_id": target_question.get("question_id"),
        "difficulty": target_question.get("difficulty"),
        "topic": target_question.get("topic"),
        "question": target_question.get("question"),
        "user_answer": user_answer,
        "result": result,
        "explanation": explanation,
        "retry_count": retry_count_llm,
        "challenge_id": session.get("challenge_id") if is_practice else None,
        "mode": "practice" if is_practice else "daily"
    })

    # 4.5 Manage Try Count for gating reveal
    try_count = session.get("try_count", 0) if is_practice else 0
    if result == "Incorrect":
        try_count += 1
        if is_practice:
            session["try_count"] = try_count
            save_user_session(chat_id, session)

    # 5. Send Feedback (Gated Reveal)
    # Save the correct answer to session so the user can reveal it via button
    session["last_correct_sql"] = correct_sql
    save_user_session(chat_id, session)

    # Keyboard with "See Answer" and "Discuss"
    gated_kb = {
        "inline_keyboard": [
            [{"text": "🔑 See Answer", "callback_data": "reveal_answer"}],
            [{"text": "💬 Discuss with AI", "callback_data": "start_discussion"}]
        ]
    }

    if result == "Correct":
        if is_practice and session.get("challenge_id"):
            explanation = f"🎊 <b>CHALLENGE SOLVED!</b> 🎊\n\nYou've successfully completed the Global Challenge! Admin has been notified of your success.\n\n" + explanation
            # Clear challenge from session after solve
            session.pop("challenge_id", None)
            save_user_session(chat_id, session)

        msg = f"<b>RESULT: {result} ✅</b>\n\n<b>EXPLANATION:</b>\n{explanation}"
        send_message(chat_id, msg, keyboard=gated_kb)
    else:
        # Incorrect
        msg = f"<b>RESULT: {result} ❌</b>\n\n<b>EXPLANATION:</b>\n{explanation}\n\n💡 <i>Try again! You can solve this. Use the hints above.</i>"
        send_message(chat_id, msg, keyboard=gated_kb)

    # 6. Session Management
    if result == "Correct":
        if is_practice:
            # Mark it as solved
            session["solved"] = True
            session["try_count"] = 0 # Reset for next question
            save_user_session(chat_id, session)
            send_message(chat_id, "🎉 <b>Great job!</b> You can discuss this query or tap 'Practice' for another.", keyboard=MAIN_MENU)
        else:
            send_message(chat_id, "✅ <b>Daily Challenge Complete!</b>", keyboard=MAIN_MENU)
        
        # Immediate Mission Check
        daily_prog = get_daily_progress(chat_id)
        if daily_prog["completed"] == 2:
            celebration = (
                "🎉 <b>Mission Complete!</b>\n"
                "You've solved 2 questions today and hit your daily goal! 🔥\n"
                "Your streak is safe. Keep it up!"
            )
            send_message(chat_id, celebration, keyboard=MAIN_MENU)
    else:
        if try_count < 2:
            send_message(chat_id, "❌ <b>Identify the mistake and try again!</b> (Attempt 1/2)")
        else:
            send_message(chat_id, "❌ <b>Keep learning!</b> Check the correct query above and see where you can improve.")

def handle_discussion_mode(chat_id, text, session):
    if text == "🛑 Stop Discussion":
        session["mode"] = "REGULAR"
        save_user_session(chat_id, session)
        send_message(chat_id, "✅ <b>Discussion ended.</b> Back to the question!", keyboard=MAIN_MENU)
        return

    # Notify processing
    send_message(chat_id, "💬 <i>Thinking...</i>")
    
    chat_history = session.get("chat_history", [])
    topic = session.get("topic", "SQL")
    question = session.get("question", "")
    
    # Calculate Background Topics for discussion
    from curriculum import get_background_topics
    base_topic = topic.split(" and ")[0]
    background_topics = get_background_topics(base_topic)
    
    # Get AI response
    response, retry_count = discuss_question(topic, question, text, chat_history, background_topics=background_topics)
    
    # Update history (keep last 10 messages for context)
    chat_history.append({"role": "user", "content": text})
    chat_history.append({"role": "assistant", "content": response})
    session["chat_history"] = chat_history[-10:]
    save_user_session(chat_id, session)
    
    # Send response
    send_message(chat_id, response, keyboard=DISCUSSION_MENU)




def handle_poll_answer(poll_answer):
    """Handle a user vote on a quiz."""
    try:
        poll_id = poll_answer["poll_id"]
        
        # 1. Capture User Info
        user_obj = poll_answer.get("user") or poll_answer.get("voter_chat")
        if not user_obj:
            print("⚠️ No user/chat info in poll_answer.")
            return

        user_id = user_obj.get("id")
        first_name = user_obj.get("first_name", "")
        username = user_obj.get("username", "")
        
        # Build a readable name
        if first_name and username:
            user_name = f"{first_name} (@{username})"
        elif first_name:
            user_name = first_name
        elif username:
            user_name = f"@{username}"
        else:
            user_name = f"User {user_id}"

        option_ids = poll_answer.get("option_ids", [])
        if not option_ids:
            # User retracted vote (if Telegram allows this in regular polls)
            return

        selected_option = option_ids[0]
        
        # Look up metadata
        meta = load_poll_metadata(poll_id)
        if not meta:
            print(f"⚠️ Unknown poll_id: {poll_id}")
            return
            
        is_correct = (selected_option == meta["correct_option_id"])
        
        print(f"🗳️ Vote received: {user_name} -> Option {selected_option} (Correct: {is_correct})")
        
        save_quiz_answer(
            poll_id, 
            user_id, 
            user_name, 
            meta["question_id"], 
            selected_option, 
            is_correct
        )
        
        # Trigger real-time update for admin
        refresh_live_quiz_history()
        
    except Exception as e:
        print(f"❌ Poll answer error: {e}")

def run_scheduler():
    """Background scheduler for Quizzes & Mission Reminders."""
    print("⏰ Scheduler started (IST Timezone).")
    ist_tz = pytz.timezone('Asia/Kolkata')
    
    # Track state
    last_run_hour = datetime.now(ist_tz).hour
    last_quiz_date = None
    last_quiz_hour = None
    
    while True:
        try:
            now = datetime.now(ist_tz)
            hour = now.hour
            today_str = now.date().isoformat()
            
            # --- 1. Daily Quiz Scheduler (9 AM, 2 PM, 8 PM) ---
            quiz_hours = [9, 14, 20]
            
            if hour in quiz_hours:
                # Check if we already ran for this specific hour today
                run_key = f"{today_str}-{hour}"
                current_last_key = f"{last_quiz_date}-{last_quiz_hour}"
                
                # Check DB persistence (incase of restart)
                was_sent_db = was_quiz_sent_recently(hour_window=1)
                
                if run_key != current_last_key and not was_sent_db:
                    print(f"⏰ Triggering Scheduled Quiz for {hour}:00...")
                    try:
                        broadcast_daily_quiz()
                        last_quiz_date = today_str
                        last_quiz_hour = hour
                    except Exception as e:
                        print(f"❌ Quiz Trigger Error: {e}")
                elif was_sent_db:
                    # Update local state so we don't keep checking DB every minute
                    last_quiz_date = today_str
                    last_quiz_hour = hour

            # --- 2. Mission Reminders (Every 2 hours between 6 AM - 11 PM) ---
            is_active_hours = 6 <= hour < 23
            is_interval = (hour % 2 == 0)
            is_new_hour = hour != last_run_hour
            
            if is_active_hours and is_interval and is_new_hour:
                pending_users = get_users_pending_mission()
                
                for user_data in pending_users:
                    user_id = user_data["user_id"]
                    count = user_data.get("daily_questions_count", 0) or 0
                    
                    # Self-deleting logic: Delete previous reminder
                    session = load_user_session(user_id) or {}
                    last_id = session.get("last_reminder_id")
                    if last_id:
                        delete_message(user_id, last_id)

                    remaining = 2 - count
                    msg = (
                        f"👋 <b>Hey there!</b>\n\n"
                        f"Just a friendly reminder to complete your daily mission.\n"
                        f"🎯 <b>Target</b>: {remaining} more question{'s' if remaining > 1 else ''} to go!\n\n"
                        f"Keep your streak alive! 🔥"
                    )
                    try:
                        res = send_message(user_id, msg)
                        # Save the new message_id to session for next deletion
                        if res and res.get("ok"):
                            session["last_reminder_id"] = res["result"]["message_id"]
                            save_user_session(user_id, session)
                    except Exception as e:
                        print(f"❌ Reminder Error: {e}")
                
                last_run_hour = hour
            
            # Sleep for 1 minute to check often (quizzes need faster precision than 10 mins)
            time.sleep(60)
            
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
            time.sleep(60)

def main():
    print("🤖 SQL Practice Bot is starting...")
    
    # 0. Initialize Database
    try:
        init_storage()
    except Exception as e:
        print(f"❌ Failed to initialize storage: {e}")
    
    # 1. Start Scheduler Thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    print("🤖 SQL Practice Bot is running...")
    offset = None
    
    while True:
        try:
            updates = get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                
                # 0. Handle Poll Answers
                if "poll_answer" in upd:
                    pa = upd["poll_answer"]
                    handle_poll_answer(pa)
                    continue
                
                # 1. Handle Callbacks
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    handle_callback(
                        cb["message"]["chat"]["id"],
                        cb["data"],
                        cb["id"],
                        cb["message"]["message_id"]
                    )
                    continue

                # 2. Handle Messages
                if "message" not in upd:
                    continue
                    
                msg = upd["message"]
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "").strip()
                
                # Extract user info for name capture
                first_name = msg["chat"].get("first_name", "")
                username_handle = msg["chat"].get("username", "")
                best_name = first_name if first_name else username_handle
                if not best_name:
                    best_name = "User"
                
                if not text:
                    continue
                
                print(f"Received message from {chat_id}: {text[:20]}...")

                try:
                    # 1. PRIORITY: Global Command & Button Hooks
                    # These should work even if in session (e.g. user wants to check stats while practicing)
                    if text == "/start":
                        handle_start(chat_id, best_name)
                        continue
                    elif text == "/menu":
                        send_message(chat_id, "📱 <b>Menu refreshed!</b>", keyboard=MAIN_MENU)
                        continue
                    elif text == "/admin":
                        handle_admin_command(chat_id, text, load_user_session(chat_id) or {})
                        continue
                    elif text == "🛑 Cancel":
                        session = load_user_session(chat_id) or {}
                        session.pop("state", None)
                        # Clear broadcast/poll/challenge data
                        for key in ["broadcast_msg", "poll_question", "poll_options", "challenge_diff", "challenge_topic", "challenge_q"]:
                            session.pop(key, None)
                        save_user_session(chat_id, session)
                        msg = "🛑 <b>Operation Cancelled</b>"
                        mode = session.get("mode")
                        kb = ADMIN_MENU if mode == "ADMIN" else MAIN_MENU
                        send_message(chat_id, msg, keyboard=kb)
                        continue

                    elif text in ["/practice", "/more", "🎯 Practice"]:
                        handle_practice(chat_id)
                        continue
                    elif text in ["🧩 Topic Wise"]:
                        handle_topic_selection_menu(chat_id)
                        continue
                    elif text in ["/profile", "/stats", "📊 Profile"]:
                        handle_profile(chat_id)
                        continue
                    elif text in ["/mission", "🎯 Mission"]:
                        handle_mission(chat_id)
                        continue
                    elif text in ["/leaderboard", "🏆 Leaderboard"]:
                        handle_leaderboard(chat_id)
                        continue
                    elif text == "📢 Feedback":
                        handle_feedback_command(chat_id, load_user_session(chat_id) or {})
                        continue

                    # 2. SECONDARY: Contextual States (Discussion, Admin, etc.)
                    session = load_user_session(chat_id)
                    
                    if session and session.get("mode") == "DISCUSSION":
                        handle_discussion_mode(chat_id, text, session)
                        continue

                    if session and session.get("state") == "awaiting_feedback":
                        handle_feedback_message(chat_id, msg["chat"].get("username", "Unknown"), text, session)
                        continue

                    # NEW: Priority for Admin Menu Buttons
                    # If the user clicks any top-level Admin button, we clear the sub-state and handle it.
                    admin_buttons = [
                        "📊 Stats", "📝 Quiz History", "📢 Broadcast", "🏆 Global Challenge", 
                        "📈 Challenge Results", "📊 Create Poll", "📊 Poll Results", "🔙 Main Menu"
                    ]
                    if session and session.get("mode") == "ADMIN" and text in admin_buttons:
                        session.pop("state", None)
                        # Clear broadcast/poll/challenge data as well
                        for key in ["broadcast_msg", "poll_question", "poll_options", "challenge_diff", "challenge_topic", "challenge_q"]:
                            session.pop(key, None)
                        save_user_session(chat_id, session)
                        handle_admin_command(chat_id, text, session)
                        continue

                    admin_states = [
                        "awaiting_broadcast", "confirm_broadcast",
                        "awaiting_poll_question", "awaiting_poll_options", "confirm_poll"
                    ]
                    if session and session.get("state") in admin_states:
                        handle_broadcast_message(chat_id, text, session)
                        continue
                    
                    if session and session.get("mode") == "ADMIN":
                        handle_admin_command(chat_id, text, session)
                        continue

                    # 3. FALLBACK: Answer Evaluation
                    handle_answer(chat_id, text)
                except Exception as e:
                    import traceback
                    error_trace = traceback.format_exc()
                    print(f"❌ Error handling message from {chat_id}: {e}")
                    
                    # 1. Notify User
                    send_message(chat_id, f"⚠️ <b>System Error</b>:\nAn error occurred while processing your request.\n\n<code>{str(e)}</code>")
                    
                    # 2. Notify Admin with full details
                    if chat_id != ADMIN_ID:
                        admin_report = (
                            f"🚨 <b>System Error Alert</b>\n\n"
                            f"👤 <b>User ID</b>: <code>{chat_id}</code>\n"
                            f"👤 <b>Name</b>: {best_name}\n"
                            f"💬 <b>Input</b>: {text[:100]}\n"
                            f"⚠️ <b>Error</b>: <code>{str(e)}</code>\n\n"
                            f"📑 <b>Traceback</b>:\n<pre>{error_trace[-3000:]}</pre>"
                        )
                        send_message(ADMIN_ID, admin_report)
                    else:
                        traceback.print_exc()
            
            time.sleep(1)
        except Exception as e:
            print(f"🌐 Polling error: {e}")
            time.sleep(5) # Wait before retrying on network error

def run_health_check():
    """Run a simple HTTP server to satisfy Render/Hugging Face health checks."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import os
    
    # Render provides PORT, Hugging Face uses 7860, default to 10000
    port = int(os.environ.get("PORT", 7860))
    
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, format, *args):
            return # Silent logs

    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"🌍 Health check server started on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    # Start health check in background
    threading.Thread(target=run_health_check, daemon=True).start()
    main()
