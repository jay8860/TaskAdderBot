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
                f"📝 **Task:** {task_data.get('task_number')}\n"
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


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    logging.info(f"Received voice note from {user_id}")
    
    await update.message.reply_text("🎧 Listening and processing...")

    file_path = None
    try:
        # 1. Download Voice File
        voice_file = await update.message.voice.get_file()
        file_path = f"temp_voice_{user_id}_{int(datetime.datetime.now().timestamp())}.ogg"
        await voice_file.download_to_drive(file_path)
        
        logging.info(f"Uploading {file_path} to Gemini...")
        myfile = genai.upload_file(file_path, mime_type="audio/ogg")
        
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        year_str = datetime.date.today().year

        raw_officers = fetch_raw_officers()
        valid_officers_prompt = get_officer_prompt_list(raw_officers)
        
        prompt = f"""
        Listen to this audio command. Today is {today_str}. (Year {year_str})
        
        VALID OFFICERS LIST:
        {json.dumps(valid_officers_prompt)}

        INSTRUCTION:
        This audio may contain one or multiple tasks.
        CRITICAL SPLITTING RULE:
        - Only split into multiple tasks if the user explicitly uses the word "Next" (e.g., "Next", "Next task", "Next one").
        - Do NOT split on "and" (e.g. "Repair the road and fix the light" should be ONE task unless "Next" is used).
        - Treat "and" as a connector within a single task description.
        
        Extract the details into a JSON LIST of objects.
        Each object in the list must have:
        - description: The full task description.
        - assigned_agency: The agency or person assigned (MATCH AGAINST VALID OFFICERS LIST).
          * RULE: You MUST return the "Official Display Name" (the part AFTER the '->').
          * EXAMPLE: If list says "Ramlal Korram -> Steno" and audio says "Ramlal", you MUST return "Steno".
          * CRITICAL: If the person to assign the task to is not clear, not mentioned, or ambiguous, assign it to "Steno" by default.
        - deadline_date: YYYY-MM-DD. (Default to null if not clear).
        - priority: High/Medium/Low.
        
        Return ONLY the JSON LIST. Example: [{{"description": "...", "assigned_agency": "Steno", ...}}]
        """
        
        result = model.generate_content([myfile, prompt])
        response_text = result.text.strip()
        
        if response_text.startswith("```json"):
            response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"):
            response_text = response_text[3:-3].strip()
            
        data_extracted = json.loads(response_text)
        logging.info(f"Extracted Data: {data_extracted}")
        
        # Normalize to list
        if isinstance(data_extracted, dict):
            task_list = [data_extracted]
        elif isinstance(data_extracted, list):
            task_list = data_extracted
        else:
            task_list = []
            
        if not task_list:
            await update.message.reply_text("⚠️ I couldn't understand any tasks from that.")
            return

        await update.message.reply_text(f"🔍 Found {len(task_list)} task(s). Processing...")

        for i, task_data in enumerate(task_list):
            # Mapping Correction per Task
            task_data['task_number'] = task_data.get('description', f'Voice Task {i+1}')
            
            # Fix Assigned Agency Name (Strict Normalization to Display Name)
            task_data['assigned_agency'] = normalize_to_display_name(raw_officers, task_data.get('assigned_agency'))

            task_data['description'] = ""
            task_data['source'] = "VoiceBot"
            task_data['allocated_date'] = today_str
            
            d1 = datetime.datetime.strptime(today_str, "%Y-%m-%d").date()
            if task_data.get('deadline_date'):
                try:
                    d2 = datetime.datetime.strptime(task_data['deadline_date'], "%Y-%m-%d").date()
                    delta = (d2 - d1).days
                    task_data['time_given'] = str(delta)
                except:
                    task_data['time_given'] = "7"
            else:
                task_data['time_given'] = "7"
                d2 = d1 + datetime.timedelta(days=7)
                task_data['deadline_date'] = d2.strftime("%Y-%m-%d")
            
            # Process and Notify
            await process_task_creation(update, task_data, raw_officers)
            # Small delay to avoid message flood/order issues
            await asyncio.sleep(1)

    except Exception as e:
        logging.error(f"Error processing voice: {e}")
        await update.message.reply_text(f"❌ Error processing voice: {str(e)}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text_content = update.message.text
    logging.info(f"Received text from {user_id}: {text_content}")
    
    await update.message.reply_text("✍️ Processing your text...")

    try:
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        year_str = datetime.date.today().year
        
        raw_officers = fetch_raw_officers()
        valid_officers_prompt = get_officer_prompt_list(raw_officers)

        prompt = f"""
        You are a smart Task Extractor. Today is {today_str} (Year {year_str}).
        
        VALID OFFICERS LIST:
        {json.dumps(valid_officers_prompt)}
        
        INSTRUCTION:
        This text may contain one or multiple tasks.
        CRITICAL SPLITTING RULE:
        - Only split into multiple tasks if the user explicitly uses the word "Next" (e.g., "Next", "Next task", "Next one").
        - Do NOT split on "and".
        - Treat "and" as a connector within a single task description.

        Analyze this command: "{text_content}"
        
        Extract the details into a JSON LIST of objects.
        Each object in the list must have:
        - description: The full task description.
        - assigned_agency: The agency or person assigned (MATCH AGAINST VALID OFFICERS LIST).
          * RULE: You MUST return the "Official Display Name" (the part AFTER the '->').
          * EXAMPLE: If list says "Ramlal Korram -> Steno" and audio says "Ramlal", you MUST return "Steno".
          * CRITICAL: If the person to assign the task to is not clear, not mentioned, or ambiguous, assign it to "Steno" by default.
        - deadline_date: YYYY-MM-DD. (Default to null).
        - priority: High/Medium/Low.
        
        Return ONLY the JSON LIST. Example: [{{"description": "...", "assigned_agency": "Steno", ...}}]
        """
        
        result = model.generate_content(prompt)
        response_text = result.text.strip()
        
        if response_text.startswith("```json"):
            response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"):
            response_text = response_text[3:-3].strip()
            
        data_extracted = json.loads(response_text)
        logging.info(f"Extracted Data: {data_extracted}")
        
        # Normalize to list
        if isinstance(data_extracted, dict):
            task_list = [data_extracted]
        elif isinstance(data_extracted, list):
            task_list = data_extracted
        else:
            task_list = []
            
        if not task_list:
             await update.message.reply_text("⚠️ I couldn't understand any tasks from that.")
             return

        await update.message.reply_text(f"🔍 Found {len(task_list)} task(s). Processing...")

        for i, task_data in enumerate(task_list):
            task_data['task_number'] = task_data.get('description', f'Task {i+1}')
            
            # Fix Assigned Agency Name (Strict Normalization to Display Name)
            task_data['assigned_agency'] = normalize_to_display_name(raw_officers, task_data.get('assigned_agency'))
            
            task_data['description'] = ""
            task_data['source'] = "VoiceBot"
            task_data['allocated_date'] = today_str
            
            d1 = datetime.datetime.strptime(today_str, "%Y-%m-%d").date()
            if task_data.get('deadline_date'):
                try:
                    d2 = datetime.datetime.strptime(task_data['deadline_date'], "%Y-%m-%d").date()
                    delta = (d2 - d1).days
                    task_data['time_given'] = str(delta)
                except:
                    task_data['time_given'] = "7"
            else:
                task_data['time_given'] = "7"
                d2 = d1 + datetime.timedelta(days=7)
                task_data['deadline_date'] = d2.strftime("%Y-%m-%d")
            
            await process_task_creation(update, task_data, raw_officers)
            await asyncio.sleep(1)

    except Exception as e:
        logging.error(f"Error processing text: {e}")
        await update.message.reply_text("❌ Something went wrong while processing your text.")

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
