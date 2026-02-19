import os
import json
from llm_helper import generate_question, evaluate_sql, discuss_question
from dotenv import load_dotenv

load_dotenv()

def test_generation():
    print("🧪 Testing Advanced Question Generation...")
    topic = "JOINs and Window Functions"
    # Mocking background topics: basic selects, simple joins
    bg = ["SELECT", "INNER JOIN", "GROUP BY"]
    
    # Generate Advanced (Sub-level 3)
    content, retries = generate_question("advanced", topic, background_topics=bg, sub_level=3)
    
    print(f"\n--- GENERATED QUESTION ---\n{content}\n")
    
    # Verification checks
    tables_count = content.count("Table:") + content.count("Tables:")
    print(f"📊 Table Definitions Found: {tables_count}")
    
    if tables_count >= 2:
        print("✅ PASS: Multi-table requirement met.")
    else:
        print("❌ FAIL: Expected 2+ tables for Advanced.")

def test_mentor():
    print("\n🧪 Testing AI Mentor Conciseness & Visuals...")
    question = "Calculate MoM growth for sales across 3 regions."
    topic = "Window Functions"
    
    response = discuss_question(question, topic, "How do I start?")
    print(f"\n--- MENTOR RESPONSE ---\n{response}\n")
    
    if "<pre>" in response:
        print("✅ PASS: ASCII visual (pre) included.")
    else:
        print("⚠️ WARNING: No ASCII visual found in this response.")

if __name__ == "__main__":
    if not os.getenv("GROQ_API_KEY"):
        print("❌ Error: GROQ_API_KEY not found. Skipping LLM tests.")
    else:
        test_generation()
        test_mentor()
