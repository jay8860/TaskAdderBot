import os
import logging
import asyncio
import json
import requests
import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import google.generativeai as genai

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_URL = os.getenv("API_URL", "http://localhost:8000/tasks/")

# --- DYNAMIC CONFIGURATION ---
def fetch_raw_officers():
    try:
        base_url = API_URL.rsplit('/', 2)[0]
        if not base_url.endswith("/api"):
             base_url = "http://localhost:8000/api"
        
        emp_url = f"{base_url}/employees/"
        logging.info(f"Fetching officers from {emp_url}")
        
        response = requests.get(emp_url, timeout=4)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logging.error(f"Failed to fetch employees: {e}")
    return []

def get_officer_prompt_list(officers):
    mapping = []
    if not officers:
        return ["Me", "Others"]
    for e in officers:
        name = e.get('name', '').strip()
        disp = e.get('display_name', '').strip()
        if name and disp:
             # FORMAT: Casual Name -> Official Display Name
             mapping.append(f"{name} -> {disp}")
        elif disp:
             mapping.append(disp)
    return mapping

def normalize_to_display_name(officers, assigned_name):
    """
    Strictly maps any incoming name (casual or display) back to the official Display Name.
    Example: 'Ramlal Korram' -> 'Steno' (if mapped)
    """
    if not assigned_name:
        return "Steno" # Default
        
    target = assigned_name.lower().strip()
    
    # 1. Check if it's already a Display Name (Direct Match)
    for e in officers:
        disp = e.get('display_name', '').strip()
        if disp.lower() == target:
            return disp
            
    # 2. Check if it's a Casual Name (Mapping Match)
    for e in officers:
        name = e.get('name', '').strip()
        if name.lower() == target:
            return e.get('display_name', name)
            
    return assigned_name # Fallback to original if no match found

def find_officer_contact(officers, assigned_name):
    """Finds mobile number for the assigned agency."""
    if not assigned_name:
        return None, None
    
    # Normalizing comparison
    target = assigned_name.lower().strip()
    
    for e in officers:
        disp = e.get('display_name', '').strip()
        if disp.lower() == target:
            return e.get('name', disp), e.get('mobile', '')
            
    return None, None

def clean_agency_name(name):
    """
    Removes the 'Name -> ' prefix if present.
    Example: 'Aditya -> Aditya DMF' becomes 'Aditya DMF'.
    """
    if not name:
        return name
    if "->" in name:
        return name.split("->")[-1].strip()
    return name.strip()


# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎙️ **Voice-to-Action Bot Active**\n\n"
        "Just send me a voice note like:\n"
        "_'Assign road repair in Geedam to PWD by next Friday.'_\n\n"
        "I will automatically create the task and ask to notify the officer."
    )

async def notification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Acknowledge
    
    data = query.data
    
    if data == "notify_send":
        # Simulate Sending
        # In a real app, we would use Twilio (SMS) or Telegram Chat ID here.
        # Since we only have Mobile Numbers, we simulate success.
        original_text = query.message.text
        # Append confirmation
        await query.edit_message_text(
            text=f"{original_text}\n\n✅ **Notification Sent!** (Simulated)\n*Note: To send real Telegram messages, Chat IDs are required.*",
            parse_mode='Markdown'
        )
    elif data == "notify_cancel":
        original_text = query.message.text
        await query.edit_message_text(
            text=f"{original_text}\n\n❌ **Notification Cancelled.**",
            parse_mode='Markdown'
        )

async def process_task_creation(update: Update, task_data: dict, officers_list: list):
    """Helper to push task to API and handle notification flow."""
    try:
        response = requests.post(API_URL, json=task_data)
        
        if response.status_code == 200 or response.status_code == 201:
            created_task = response.json()
            assigned_to = created_task.get('assigned_agency')
            
            reply = (
                f"✅ **Task Created!**\n\n"
                f"📝 **Task:** {created_task.get('task_number')}\n"
                f"👤 **Assigned:** {assigned_to or 'Unassigned'}\n"
                f"📅 **Deadline:** {created_task.get('deadline_date') or 'No Deadline'}"
            )
            await update.message.reply_text(reply, parse_mode='Markdown')
            
            # --- NOTIFICATION FLOW DISABLED (User Request) ---
            # if assigned_to:
            #     name, mobile = find_officer_contact(officers_list, assigned_to)
            #     if mobile:
            #         logging.info(f"Notification skipped for {name} ({mobile}) as per config.")
            #     else:
            #         logging.info(f"No mobile found for {assigned_to}")
            
        else:
            await update.message.reply_text(f"⚠️ Failed to create task via API.\nStatus: {response.status_code}\nError: {response.text}")

    except Exception as e:
         logging.error(f"API Push Error: {e}")
         await update.message.reply_text(f"❌ Error saving task: {str(e)}")


async def handle_core_logic(update: Update, prompt_input: str, is_voice: bool = False, file_path: str = None):
    """Unified logic for voice and text processing."""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    year_str = datetime.date.today().year
    raw_officers = fetch_raw_officers()
    valid_officers_prompt = get_officer_prompt_list(raw_officers)
    
    # 1. Intent Detection & Translation Prompt (Using concatenation for safety)
    intent_prompt = "You are a smart Task Assistant. Today is " + today_str + " (Year " + str(year_str) + ").\n\n"
    intent_prompt += "VALID OFFICERS LIST:\n" + json.dumps(valid_officers_prompt) + "\n\n"
    intent_prompt += """INSTRUCTIONS:
    1. Detect INTENT: "CREATE" (log new task) or "QUERY" (ask about tasks).
    2. TRANSLATION: If the user speaks in Hindi, you MUST translate it into a professional, grammatically correct English sentence.
       - STRICT RULE: DO NOT use 'Hinglish' or transliterated Hindi (e.g., "bolna", "kar dena", "dega" are FORBIDDEN).
       - INSTEAD Use proper English (e.g., "Instruct Steno to...", "Prepare a chart...", "By Monday...").
       - EXAMPLE: If user says "Steno ko bolna chart bana de", you MUST return "Instruct Steno to prepare the chart."
    3. FOR "CREATE": Extract details into a JSON list.
       - Description: The task description translated into MEANINGFUL, PROFESSIONAL English sentence.
       - assigned_agency: You MUST return ONLY the "Official Display Name" (the part AFTER the '->' in the list).
         * CRITICAL: If you match a person, use their Display Name. 
         * EXAMPLE: If list says "Ramlal Korram -> Steno", return "Steno". Never return "Ramlal Korram".
         * DEFAULT: Use "Steno" if unclear.
       - deadline_date: YYYY-MM-DD.
       - priority: High/Medium/Low.
    4. FOR "QUERY": Extract search parameters into a JSON object.
       - search_query: The user's question translated into English.

    Return ONLY JSON:
    {
      "intent": "CREATE" | "QUERY",
      "data": [...] or {"search_query": "..."}
    }
    """
    
    try:
        if is_voice and file_path:
             logging.info(f"Uploading {file_path} to Gemini...")
             myfile = genai.upload_file(file_path, mime_type="audio/ogg")
             result = model.generate_content([myfile, intent_prompt])
        else:
             result = model.generate_content("Analyze this command: \"" + prompt_input + "\"\n\n" + intent_prompt)
             
        response_text = result.text.strip()
        if response_text.startswith("```json"): response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"): response_text = response_text[3:-3].strip()
        
        classification = json.loads(response_text)
        intent = classification.get("intent")
        data = classification.get("data")
        
        logging.info(f"Detected Intent: {intent}")

        if intent == "CREATE":
            task_list = data if isinstance(data, list) else [data]
            if not task_list:
                await update.message.reply_text("⚠️ I couldn't understand any tasks from that.")
                return

            await update.message.reply_text(f"🔍 Found {len(task_list)} task(s). Processing...")
            for i, task_data in enumerate(task_list):
                task_desc = task_data.get('description', f'Task {i+1}')
                task_data['task_number'] = None # Let Backend Auto-Generate
                task_data['assigned_agency'] = normalize_to_display_name(raw_officers, task_data.get('assigned_agency'))
                if not task_data.get('description'):
                    task_data['description'] = task_desc
                task_data['source'] = "VoiceBot"
                task_data['allocated_date'] = today_str
                
                # Deadline Logic
                d1 = datetime.datetime.strptime(today_str, "%Y-%m-%d").date()
                if task_data.get('deadline_date'):
                    try:
                        d2 = datetime.datetime.strptime(task_data['deadline_date'], "%Y-%m-%d").date()
                        task_data['time_given'] = str((d2 - d1).days)
                    except: task_data['time_given'] = "7"
                else:
                    task_data['time_given'] = "7"
                    task_data['deadline_date'] = (d1 + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                
                await process_task_creation(update, task_data, raw_officers)
                await asyncio.sleep(0.5)

        elif intent == "QUERY":
            await update.message.reply_text("🔎 Searching database...")
            # Fetch all tasks for context
            resp = requests.get(API_URL)
            if resp.status_code == 200:
                all_tasks = resp.json()
                # Limit context size (e.g. last 100 tasks or active ones)
                context_tasks = all_tasks[-100:] 
                
                context_json = json.dumps([{ 'task': t['task_number'], 'assigned': t['assigned_agency'], 'status': t['status'], 'deadline': t['deadline_date'] } for t in context_tasks])
                
                query_prompt = "User Question: \"" + str(data.get('search_query', prompt_input)) + "\"\n\n"
                query_prompt += "REAL-TIME TASK DATA (CONTEXT):\n" + context_json + "\n\n"
                query_prompt += """INSTRUCTION:
                Answer the user's question based ONLY on the provided context. 
                Be concise and helpful. Use Markdown for formatting.
                """
                answer = model.generate_content(query_prompt)
                await update.message.reply_text(answer.text, parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Failed to fetch task data for search.")

    except Exception as e:
        logging.error(f"Logic Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text("🎧 Listening and processing...")
    file_path = f"temp_voice_{user_id}_{int(datetime.datetime.now().timestamp())}.ogg"
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)
        await handle_core_logic(update, "", is_voice=True, file_path=file_path)
    finally:
        if file_path and os.path.exists(file_path): os.remove(file_path)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✍️ Processing...")
    await handle_core_logic(update, update.message.text)

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    # Callback Handler
    application.add_handler(CallbackQueryHandler(notification_callback))
    
    print("Voice & Text Bot Started...")
    application.run_polling()

if __name__ == '__main__':
    main()
