
import os
from groq import Groq
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Groq client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODEL_NAME = "llama-3.3-70b-versatile"

def sanitize_html(text):
    """
    Sanitize HTML in LLM responses to prevent Telegram parse errors.
    Bulletproof approach: Escape everything, then restore allowed tags.
    """
    import re
    
    # 1. First, escape all ampersands
    text = text.replace('&', '&amp;')
    
    # NEW: Proactive Markdown-to-HTML Conversion
    # Convert bold (**) to <b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Convert code (`) to <code>
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Convert underscores (_) to <i> if they look like italics (surrounded by spaces or start/end of line)
    text = re.sub(r'(^|\s)_(.*?)_($|\s)', r'\1<i>\2</i>\3', text)

    # NEW: Pre-processing for lists (Auto-convert forbidden tags to manual formatting)
    # Replace (u/o)l tags with nothing, just use their contents
    text = re.sub(r'</?\s*[uo]l\s*>', '\n', text, flags=re.IGNORECASE)
    # Replace li tags with a newline and a manual bullet
    text = re.sub(r'<li\s*>', '\n• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '', text, flags=re.IGNORECASE)
    
    # 2. Identify allowed tags precisely and hide them
    # Allowed: <b>, <i>, <code>, <pre> (case-insensitive, optional spaces)
    allowed_tags = ['b', 'i', 'code', 'pre']
    placeholders = {}
    
    def tag_to_placeholder(match):
        tag = match.group(0).lower().replace(' ', '')
        placeholder = f"___HTAG_{len(placeholders)}___"
        placeholders[placeholder] = tag
        return placeholder

    # Regex to find tags like <b>, < b >, </ b >, <B>, etc.
    tag_pattern = re.compile(r'</?\s*(?:b|i|code|pre)\s*>', re.IGNORECASE)
    text = tag_pattern.sub(tag_to_placeholder, text)
    
    # 3. Escape all remaining brackets (< and >)
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    
    # 4. Restore the hidden allowed tags
    for placeholder, tag in placeholders.items():
        text = text.replace(placeholder, tag)
    
    # 5. Fix double-escaped ampersands for common entities
    text = text.replace('&amp;amp;', '&amp;').replace('&amp;lt;', '&lt;').replace('&amp;gt;', '&gt;')
    
    # 6. Ensure all tags are properly closed (stack-based for correctness)
    for tag in allowed_tags:
        open_count = text.count(f'<{tag}>')
        close_count = text.count(f'</{tag}>')
        if open_count > close_count:
            text += f'</{tag}>' * (open_count - close_count)
        elif close_count > open_count:
            for _ in range(close_count - open_count):
                text = re.sub(rf'</{tag}>', '', text, count=1)
    
    return text

def parse_feedback(text):
    result = "Unknown"
    explanation = "No explanation."
    correct_sql = ""

    if "RESULT:" in text:
        line = text.split("RESULT:")[1].splitlines()[0]
        result = "Correct" if "Correct" in line else "Incorrect"

    if "EXPLANATION:" in text:
        explanation = (
            text.split("EXPLANATION:")[1]
            .split("CORRECT_SQL:")[0]
            .strip()
        )
        
    if "CORRECT_SQL:" in text:
        correct_sql = text.split("CORRECT_SQL:")[1].strip()

    return result, explanation, correct_sql

def validate_response(content, expected_type):
    """
    Validates the LLM response for forbidden tags, markdown, and structure.
    Returns (is_valid, error_message).
    """
    import re
    
    # 1. Forbidden Tags
    forbidden = ['table', 'ul', 'li', 'span', 'div']
    for tag in forbidden:
        if re.search(rf'</?\s*{tag}\s*>', content, re.IGNORECASE):
            return False, f"Found forbidden HTML tag: <{tag}>. Please use manual formatting (e.g., • for lists)."

    # 2. Markdown Check (Comprehensive)
    # Ban any asterisks or underscores which are common Markdown markers
    if "*" in content:
        return False, "FORBIDDEN: Found asterisks (*). Do NOT use Markdown (e.g., **bold**). Use ONLY HTML tags like <b>."
    if "_" in content and not re.search(r'[a-zA-Z0-9]_|[a-zA-Z0-9]_', content):
        # We allow underscores if they look like part of a column/table name (e.g. user_id)
        # But standalone underscores like _italic_ or word _ word are banned.
        if re.search(r'\s_\w|_\s', content):
            return False, "FORBIDDEN: Found Markdown italics (_). Use <i> instead."
    
    # 3. Code tag check - ensure no backticks
    if "`" in content:
         return False, "FORBIDDEN: Found backticks (`). Use <code> or <pre> instead."
    
    # 4. Length Check
    if len(content) > 4000:
        return False, f"FORBIDDEN: Response is too long ({len(content)} characters). Telegram limit is 4096. Please be more concise and stay under 3000 characters."

    # 5. Global Wrapping Check
    if content.strip().startswith('<pre>') and content.strip().endswith('</pre>'):
        # Check if there's only one pre block
        if content.count('<pre>') == 1:
            return False, "FORBIDDEN: The entire response is wrapped in <pre>. Only the SQL/Data should be in <pre>."

    # 6. Structure Check
    if expected_type == "question":
        required = ["Problem Description:", "Tables:"]
        for req in required:
            if req not in content:
                return False, f"Missing required section: {req}"
    
    elif expected_type == "evaluation":
        required = ["RESULT:", "EXPLANATION:", "CORRECT_SQL:"]
        for req in required:
            if req not in content:
                return False, f"Missing required section: {req}"
        # Check for code blocks in explanation
        explanation_part = content.split("EXPLANATION:")[1].split("CORRECT_SQL:")[0]
        if "<pre>" in explanation_part or "<code>" in explanation_part:
             return False, "FORBIDDEN: Found code blocks (<pre>/<code>) inside the EXPLANATION section. Use <b> for SQL snippets in prose."

    return True, ""

def evaluate_sql(question, user_answer, background_topics=None):
    if background_topics is None:
        background_topics = []
    bg_list = ", ".join(background_topics) if background_topics else "None (Tutorial level)"

    prompt = f"""
You are an expert SQL evaluator.

<b>EVALUATION POLICY (STRICT):</b>
- <b>CONCISENESS</b>: Max 2 paragraphs for EXPLANATION. 
- <b>NO FILLER</b>: Do not say "I've reviewed your query" or "Here is my feedback".
- <b>VISUAL BRAKDOWN (MANDATORY)</b>: Use small ASCII diagrams (in <pre>) if the logic involves JOINs, filters, or aggregations to show why it failed.
  Example failure visual:
  <pre>
  Expected: [Data A] --(Filter X)--> [Result]
  User Got: [Data A] --(Filter Y)--> [Result]
  </pre>

<b>Output Requirements:</b>
1. <b>EXPLANATION SECTION</b>: Use <b>bold</b> for SQL snippets. NO code blocks.
2. <b>CORRECT_SQL SECTION</b>: Use <code>&lt;pre&gt;&lt;code&gt;</code>.
3. <b>DIALECT</b>: MySQL.

<b>Context:</b>
Question: {question}
User Answer: {user_answer}

<b>Output Format:</b>
RESULT: <b>Correct</b> / <b>Incorrect</b> [Visual Emoji]
EXPLANATION: [Brief breakdown + ASCII visual in <pre> if helpful]
CORRECT_SQL: 
<pre><code>[The ideal query]</code></pre>
"""

    retry_count = 0
    max_retries = 3
    messages = [{"role": "user", "content": prompt}]
    
    while retry_count < max_retries:
        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.2
            )
            feedback = response.choices[0].message.content
            
            # 1. Sanitize & Validate (Sanitize first to auto-fix minor issues)
            feedback = sanitize_html(feedback)
            is_valid, error_msg = validate_response(feedback, "evaluation")
            if is_valid:
                result, explanation, correct_sql = parse_feedback(feedback)
                return feedback, result, explanation, correct_sql, retry_count
            
            # 3. Handle Invalid
            retry_count += 1
            messages.append({"role": "assistant", "content": feedback})
            messages.append({"role": "user", "content": f"ERROR: {error_msg}\nPlease fix these formatting errors and resubmit the evaluation strictly following the rules."})
            print(f"Retry {retry_count} for evaluation: {error_msg}")
            
        except Exception as e:
            print(f"Error evaluating SQL: {e}")
            break
            
    # Final Fallback: Super Clean
    feedback = sanitize_html(feedback)
    result, explanation, correct_sql = parse_feedback(feedback)
    return feedback, result, explanation, correct_sql, retry_count


def generate_question(difficulty, topic, background_topics=None, sub_level=1, past_questions=None):
    if background_topics is None or not hasattr(background_topics, '__iter__') or isinstance(background_topics, (str, bytes)):
        background_topics = []
        
    # Format background topics for the prompt
    bg_list = ", ".join(background_topics) if background_topics else "None (Tutorial level)"
    
    # Anti-Duplication
    dupe_msg = ""
    if past_questions:
        dupe_msg = f"<b>CRITICAL: Avoid These Recent Scenarios:</b>\n" + "\n".join([f"- {q[:100]}..." for q in past_questions[:15]]) + "\n"

    # Sub-level descriptions for LLM
    level_names = {1: "EASY", 2: "MEDIUM", 3: "HARD"}
    current_level_name = level_names.get(sub_level, "EASY")
    
    level_constraints = {
        1: "Focus on basic syntax and single-table queries. Keep the business logic simple and direct.",
        2: "Increase complexity. Integrate filtering, simple aggregations, and ALWAYS require 2-3 tables with JOINs.",
        3: "Principal Level. Use complex business scenarios involving 3-4 tables, nested logic, window-like filters (without necessarily using window functions if not in tools), or complex date arithmetic."
    }
    current_constraint = level_constraints.get(sub_level, level_constraints[1])

    prompt = f"""
You are a Principal SQL Interviewer at a top tech company. Your task is to generate one high-quality, professional SQL interview question at the <b>{current_level_name}</b> level.

{dupe_msg}

<b>LEETCODE STYLE EXAMPLE:</b>
<b>Problem Description:</b> A logistics company needs to find "High-Value Routes." A route is high-value if it has more than 10 shipments in the last 30 days and the average package weight is above 50kg.
<b>Tables:</b> 
<pre>
Table: routes (PK: route_id)
| route_id | origin_city | dest_city |
| 1        | New York    | London    |
| 2        | Tokyo       | Paris     |

Table: shipments (PK: ship_id, FK: route_id)
| ship_id | route_id | weight | ship_date  |
| 100     | 1        | 60.5   | 2024-01-05 |
| 101     | 1        | 45.0   | 2024-01-10 |
</pre>

<b>INTERNAL CHALLENGE CONSTRAINTS (STRICT):</b>
- <b>Target Topic to Test</b>: {topic}
- <b>Sub-Level Complexity</b>: {current_level_name} ({current_constraint})
- <b>Available Syntax Tools</b>: {bg_list}
- <b>TABLE COUNT</b>: For {current_level_name}, you MUST use **{1 if sub_level==1 else ('2-3' if sub_level==2 else '3-4')} tables**. 
- <b>SCHEMA CONSTRAINTS</b>: Always briefly mention Primary Keys (PK) and Foreign Keys (FK) for each table.

<b>CRITICAL RULES:</b>
1. <b>NO META-TALK</b>: Never mention "internal briefs," "learned topics," or "syntax constraints."
2. <b>NO LOGIC HINTS / PSEUDO-SQL</b>:
    - NEVER use phrases like "between X and Y", "join table A with B", "group by", "filter rows where".
    - <b>INSTEAD</b>: Use business language. (e.g., "Find the most active accounts" instead of "Filter for login_count > 10").
3. <b>NO HINTS / HAND-HOLDING</b>:
    - NEVER tell the user which logic to use (e.g., "Use a Subquery").
    - The goal is to test if the user knows *how* to translate business requirements into SQL.
4. <b>ANTI-REPETITION</b>: Use creative business domains (Renewable Energy, Healthcare, Gaming, Space Logistics). Avoid generic "Employees/Departments" unless forced by the topic.
5. <b>LOGICAL CONSISTENCY</b>: Ensure output requirements match the data provided. Total response must be under 3000 characters.

<b>Output Structure:</b>
<b>Problem Description:</b> [Define the business context and the final goal.]
<b>Tables:</b> [ASCII grids in &lt;pre&gt; showing sample data for each table.]
"""

    retry_count = 0
    max_retries = 3
    messages = [{"role": "user", "content": prompt}]
    
    while retry_count < max_retries:
        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.7
            )
            content = response.choices[0].message.content.strip()
            
            # NEW: Sanitize FIRST, then validate
            content = sanitize_html(content)
            is_valid, error_msg = validate_response(content, "question")
            if is_valid:
                return content, retry_count
                
            retry_count += 1
            messages.append({"role": "assistant", "content": content})
            
            # Specific feedback for long messages
            if "too long" in error_msg:
                error_msg += " Suggestion: Shorten the Scenario or reduce the number of sample rows in the Tables section to save space."
                
            messages.append({"role": "user", "content": f"ERROR: {error_msg}\nPlease fix the question formatting."})
            print(f"Retry {retry_count} for generation: {error_msg}")
            
        except Exception as e:
            print(f"Error generating question: {e}")
            break
            
    # Final Fallback: Super Clean
    content = sanitize_html(content)
    return content, retry_count

def discuss_question(topic, question, user_message, chat_history=None, background_topics=None):
    if chat_history is None:
        chat_history = []
    
    if background_topics is None:
        background_topics = []
    bg_list = ", ".join(background_topics) if background_topics else "None (Tutorial level)"
        
    messages = [
        {"role": "system", "content": f"""
You are a Socratic SQL Mentor. Your goal is to help the user solve the problem WITHOUT giving the final SQL.

<b>CONCISENESS POLICY (STRICT):</b>
- Keep responses under 2-3 short paragraphs.
- <b>NO FILLER</b>: Never say "I'd be happy to help", "Great question", or "Let's dive in". Just start the help.
- If the user is on the right track, just give a 1-sentence confirmation and the next hint.

<b>VISUALS POLICY (MANDATORY):</b>
- Use tiny ASCII diagrams or tables (wrapped in <code>&lt;pre&gt;</code>) to explain data flow or JOIN logic.
- Example: 
<pre>
Table A --[JOIN on ID]--> Table B
(IDs match) -> (Row kept)
</pre>

<b>Formatting Rules (STRICT):</b>
1. <b>STRICT HTML</b>: Use only <b>, <i>, <code>, <pre>.
2. <b>NO GLOBAL WRAPPING</b>: Never wrap your whole reply in <pre>.
3. <b>PROSE TEXT</b>: Use plain text for explanations. Use <b>bold</b> for SQL snippets in prose. 
4. <b>ZERO MARKDOWN POLICY</b>: No asterisks, underscores, or backticks.
5. <b>LIST POLICY</b>: Use literal bullet characters (•). Every bullet MUST start on a new line.

<b>Current Topic:</b> {topic}
<b>Question Context:</b>
{question}

<b>DIALECT:</b> MySQL
"""}
    ]
    
    # Add history
    for msg in chat_history:
        messages.append(msg)
        
    # Add new user message
    messages.append({"role": "user", "content": user_message})
    
    retry_count = 0
    max_retries = 3
    
    while retry_count < max_retries:
        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.7
            )
            content = response.choices[0].message.content.strip()
            # NEW: Sanitize FIRST, then validate
            content = sanitize_html(content)
            
            is_valid, error_msg = validate_response(content, "discussion")
            if is_valid:
                return content, retry_count
                
            retry_count += 1
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"ERROR: {error_msg}\nPlease fix the formatting."})
            print(f"Retry {retry_count} for discussion: {error_msg}")
            
        except Exception as e:
            print(f"Error in discussion: {e}")
            break
            
    # Final Fallback: Super Clean
    content = sanitize_html(content)
    return content, retry_count

# ================= QUIZ GENERATION =================

def generate_quiz_question(difficulty="intermediate", topic="random", past_questions=None):
    """
    Generates a multiple-choice SQL question (JSON format) for Telegram Polls.
    """
    import json
    
    # Anti-Duplication
    dupe_msg = ""
    if past_questions:
        # Provide MORE global history to ensure variety
        dupe_msg = f"<b>CRITICAL: Avoid These Previous Questions (Global History):</b>\n" + "\n".join([f"- {q}" for q in past_questions[:15]]) + "\n"

    prompt = f"""
You are a Principal SQL Interviewer. Generate a **Scenario-Based** multiple-choice question (MCQ) for a "Daily SQL Quiz".

<b>Target Audience:</b> {difficulty.upper()} level SQL developers.
<b>Topic:</b> {topic}
{dupe_msg}

<b>Strict Rules (High Quality):</b>
1. <b>NO Definitions</b>: Do NOT ask "What is valid syntax?" or "What does keyword X do?".
2. <b>Scenario First</b>: Start with a mini-scenario (e.g., "You are analyzing e-commerce orders...", "A junior dev wrote this query...").
3. <b>ANTI-REPETITION (STRICT)</b>: You MUST create a scenario that is DIFFERENT from the ones listed in the history above. Use different table names, business contexts, and edge cases.
4. <b>Code Snippets</b>: If the topic is complex (JOINs, WINDOW Functions, CTEs), you MUST currently include a small SQL snippet or table preview in the question.
5. <b>Realistic Distractors</b>: The wrong options must be common mistakes (e.g., forgetting to handle NULLs, confusing WHERE vs HAVING, logical off-by-one errors). Do NOT use nonsense options.

<b>Output Format (JSON ONLY):</b>
{{
    "question": "The scenario and question text. PLAIN TEXT ONLY. Do NOT use <pre> or any HTML. Write code snippets naturally.",
    "options": ["Option A (Correct)", "Option B (Plausible Mistake)", "Option C (Edge Case Error)", "Option D (Syntax Error)"], 
    "correct_option_id": 0,
    "explanation": "Explain the logic. Mention WHY the distractors are wrong if possible."
}}

<b>CRITICAL:</b> 
- <b>NO HTML IN QUESTION</b>: The Telegram Poll 'question' field does NOT support HTML. 
- Keep the `question` field under 300 characters.
- Keep `options` under 60 characters each.
"""

    messages = [
        {"role": "system", "content": "You are a JSON-generating API for SQL Quizzes."},
        {"role": "user", "content": prompt}
    ]

    retry_count = 0
    max_retries = 3

    while retry_count < max_retries:
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.7,
                max_tokens=600  # slightly more token room for complex questions
            )
            content = response.choices[0].message.content.strip()
            
            # Clean possible markdown
            if content.startswith("```json"):
                content = content.replace("```json", "").replace("```", "")
            
            # Parse JSON
            quiz_data = json.loads(content)
            
            # Validate JSON structure
            required_keys = ["question", "options", "correct_option_id", "explanation"]
            if not all(key in quiz_data for key in required_keys):
                raise ValueError("Missing keys in JSON")
            if len(quiz_data["options"]) != 4:
                raise ValueError("Must have exactly 4 options")
            
            # Telegram Limits (Strict)
            if len(quiz_data["question"]) > 300:
                raise ValueError(f"Question too long ({len(quiz_data['question'])} > 300 chars)")
            if len(quiz_data["explanation"]) > 200:
                raise ValueError(f"Explanation too long ({len(quiz_data['explanation'])} > 200 chars)")
            
            # Strip HTML from Question (Double Safety)
            import re
            quiz_data["question"] = re.sub(r'<[^>]+>', '', quiz_data["question"])
                
            return quiz_data, retry_count
            
        except (Exception) as e:
            print(f"JSON Error (Attempt {retry_count+1}): {e}")
            retry_count += 1
            
    # Fallback if AI fails repeatedly
    import random
    fallback_pool = [
        {
            "question": "Which SQL keyword is used to remove duplicate rows?",
            "options": ["DISTINCT", "UNIQUE", "DIFFERENT", "DEDUP"],
            "correct_option_id": 0,
            "explanation": "DISTINCT eliminates duplicate rows from the results."
        },
        {
            "question": "What is the default sorting order of the ORDER BY clause?",
            "options": ["Ascending (ASC)", "Descending (DESC)", "Random", "None"],
            "correct_option_id": 0,
            "explanation": "By default, ORDER BY sorts data in ascending order."
        },
        {
            "question": "Which function is used to count the number of rows in a table?",
            "options": ["COUNT()", "SUM()", "NUMBER()", "TOTAL()"],
            "correct_option_id": 0,
            "explanation": "COUNT(*) counts all rows, while COUNT(column) counts non-NULL values."
        },
        {
            "question": "Which clause is used to filter records AFTER grouping?",
            "options": ["HAVING", "WHERE", "GROUP BY", "ORDER BY"],
            "correct_option_id": 0,
            "explanation": "HAVING filters aggregated groups, whereas WHERE filters individual rows before grouping."
        },
        {
            "question": "What does a LEFT JOIN return?",
            "options": ["All left rows + matched right rows", "All right rows + matched left rows", "Only matched rows", "Cartesian product"],
            "correct_option_id": 0,
            "explanation": "LEFT JOIN returns all records from the left table and matched records from the right table (or NULL)."
        }
    ]
    return random.choice(fallback_pool), retry_count

