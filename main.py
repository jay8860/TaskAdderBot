import os
import logging
import asyncio
import json
import requests
import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import google.generativeai as genai

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_URL = os.getenv("API_URL", "http://localhost:8000/tasks/")

# --- CONFIGURATION FROM SHEET ---
VALID_OFFICERS = [
    "AC Tribal", "Aditya DMF", "Alka DMF", "All CEOs", "All CEOs + LDM", "Amit Skill", 
    "APO Tarun", "APO NREGA", "BC PMAY Geedam", "CEO JP Dantewada", "CEO JP Geedam", 
    "CEO JP Katekalyan", "CEO JP Kuakonda", "CMHO", "CMO Dantewada", "CS Abhay", "CSEB", 
    "CSSDA", "DC PMAY", "DC SBM Mamta", "DD Agri", "DD Vet", "DD Fisheries", "DD Social Welfare", 
    "DDP", "DDP + CEO JP Dante", "DEO", "Divya PPIA", "DMC", "DPM Livelihood", "DPM MIS", 
    "DPM NRLM", "DPM SMIB", "DPO WCD", "EDM", "EE PWD", "EE RES", "EE RES and Vineet Te", 
    "Korram Steno", "LDM and EDM", "Me", "Others", "PO Manoj", "PMGSY", "Pradeep Sports", 
    "Praneeth", "Principal Livelihood", "PWD EnM", "PWD SDO Ram", "Sachivs", "SDM Geedam", 
    "SDMs", "Sudama", "APO Niramn"
]


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
        "I will automatically create the task in your dashboard."
    )

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    logging.info(f"Received voice note from {user_id}")
    
    await update.message.reply_text("🎧 Listening and processing...")

    try:
        # 1. Download Voice File
        voice_file = await update.message.voice.get_file()
        file_path = f"temp_voice_{user_id}_{int(datetime.datetime.now().timestamp())}.ogg"
        await voice_file.download_to_drive(file_path)
        
        # 2. Transcribe & Extract with Gemini
        # We upload the audio file directly to Gemini for multimodal processing
        logging.info(f"Uploading {file_path} to Gemini...")
        
        # Explicitly set mime_type for Telegram OGG/Opus audio
        myfile = genai.upload_file(file_path, mime_type="audio/ogg")
        
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        year_str = datetime.date.today().year

        prompt = f"""
        Listen to this audio command. Today is {today_str}.
        
        VALID OFFICERS LIST:
        {json.dumps(VALID_OFFICERS)}

        Extract the following details into a JSON object:
        - description: The full task description.
        - assigned_agency: The agency or person assigned. 
          * MATCHING RULE: strict fuzzy match against VALID OFFICERS LIST.
          * Example: "Aditya" -> "Aditya DMF", "Meena Horti" -> "DD Horti" (if relatable).
          * If user says "me", "myself" -> "Me".
          * If the name sounds similar to a valid officer, use the valid officer name.
          * If NO match found, use the name exactly as spoken.
          * If not specified, null.
        - deadline_date: The deadline date in YYYY-MM-DD format. 
          * Calculate based on context (e.g., "next Friday"). 
          * CRITICAL: Assume year is {year_str} unless explicitly stated otherwise.
          * If "tomorrow", use date+1.
          * If not specified, null.
        - priority: High, Medium, or Low. Infer from urgency.
        
        Return ONLY the JSON.
        """
        
        result = model.generate_content([myfile, prompt])
        response_text = result.text.strip()
        
        # Clean up JSON if wrapped in markdown
        if response_text.startswith("```json"):
            response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"):
            response_text = response_text[3:-3].strip()
            
        task_data = json.loads(response_text)
        logging.info(f"Extracted Data: {task_data}")
        
        # --- DATA MAPPING CORRECTION ---
        task_data['task_number'] = task_data.get('description', 'Voice Task')
        task_data['description'] = ""
        task_data['source'] = "VoiceBot"
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        task_data['allocated_date'] = today_str
        
        # Calculate 'time_given' and handle defaults
        d1 = datetime.datetime.strptime(today_str, "%Y-%m-%d").date()
        
        if task_data.get('deadline_date'):
            try:
                d2 = datetime.datetime.strptime(task_data['deadline_date'], "%Y-%m-%d").date()
                delta = (d2 - d1).days
                task_data['time_given'] = str(delta)
            except:
                task_data['time_given'] = "7"
        else:
            # User Request: If no deadline, default Time Given to 7 days
            task_data['time_given'] = "7"
            # Calculate deadline for DB (so Dashboard works immediately)
            d2 = d1 + datetime.timedelta(days=7)
            task_data['deadline_date'] = d2.strftime("%Y-%m-%d")
        
        # 4. Push to API
        response = requests.post(API_URL, json=task_data)
        
        # Clean up file
        os.remove(file_path)
        
        if response.status_code == 200 or response.status_code == 201:
            # Success
            created_task = response.json()
            reply = (
                f"✅ **Task Created!**\n\n"
                f"📝 **Task:** {task_data.get('task_number')}\n"
                f"👤 **Assigned:** {created_task.get('assigned_agency') or 'Unassigned'}\n"
                f"📅 **Deadline:** {created_task.get('deadline_date') or 'No Deadline'}"
            )
            await update.message.reply_text(reply, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"⚠️ Failed to create task via API.\nStatus: {response.status_code}\nError: {response.text}")

    except Exception as e:
        logging.error(f"Error processing voice: {e}")
        await update.message.reply_text(f"❌ Error processing voice: {str(e)}")
        if os.path.exists(file_path):
            os.remove(file_path)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text_content = update.message.text
    logging.info(f"Received text from {user_id}: {text_content}")
    
    await update.message.reply_text("✍️ Processing your text...")

    try:
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        year_str = datetime.date.today().year
        prompt = f"""
        You are a smart Task Extractor. Today is {today_str} (Year {year_str}).
        
        VALID OFFICERS LIST:
        {json.dumps(VALID_OFFICERS)}
        
        Analyze this command: "{text_content}"
        
        Extract the following details into a JSON object:
        - description: The full task description.
        - assigned_agency: The agency or person assigned. 
          * MATCHING RULE: strict fuzzy match against VALID OFFICERS LIST.
          * Example: "Aditya" -> "Aditya DMF", "Meena Horti" -> "DD Horti" (if relatable).
          * If user says "me", "myself" -> "Me".
          * If the name sounds similar to a valid officer, use the valid officer name.
          * If NO match found, use the name exactly as written.
          * If not specified, null.
        - deadline_date: The deadline date in YYYY-MM-DD format. 
          * CRITICAL: Start with the year {year_str}.
          * If audio says "today", use {today_str}. 
          * If "tomorrow", use date+1. 
          * If "next week", use date+7. 
          * If month is mentioned (e.g., "August 24"), assume {year_str} unless a different year is explicitly said.
          * Do NOT default to a week if not specified. Default to null.
        - priority: High, Medium, or Low. Infer from urgency.
        
        Return ONLY the JSON.
        """
        
        result = model.generate_content(prompt)
        response_text = result.text.strip()
        
        # Clean up JSON if wrapped in markdown
        if response_text.startswith("```json"):
            response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"):
            response_text = response_text[3:-3].strip()
            
        task_data = json.loads(response_text)
        logging.info(f"Extracted Data: {task_data}")
        
        # --- DATA MAPPING CORRECTION ---
        # User wants the Task Description in "Task/File No" column (task_number).
        task_data['task_number'] = task_data.get('description', 'Task')
        
        # Description field (Col 4 - Notes) must be EMPTY as per user request
        task_data['description'] = ""
        
        task_data['source'] = "VoiceBot"
        task_data['allocated_date'] = today_str
        
        # Calculate 'time_given' and handle defaults
        d1 = datetime.datetime.strptime(today_str, "%Y-%m-%d").date()
        
        if task_data.get('deadline_date'):
            try:
                d2 = datetime.datetime.strptime(task_data['deadline_date'], "%Y-%m-%d").date()
                delta = (d2 - d1).days
                task_data['time_given'] = str(delta)
            except:
                task_data['time_given'] = "7"
        else:
            # User Request: If no deadline, default Time Given to 7 days
            task_data['time_given'] = "7"
            # Calculate deadline for DB (so Dashboard works immediately)
            d2 = d1 + datetime.timedelta(days=7)
            task_data['deadline_date'] = d2.strftime("%Y-%m-%d")
        
        # Push to API
        response = requests.post(API_URL, json=task_data)
        
        if response.status_code == 200 or response.status_code == 201:
            # Success
            created_task = response.json()
            reply = (
                f"✅ **Task Created!**\n\n"
                f"📝 **Task:** {task_data.get('task_number')}\n"
                f"👤 **Assigned:** {created_task.get('assigned_agency') or 'Unassigned'}\n"
                f"📅 **Deadline:** {created_task.get('deadline_date') or 'No Deadline'}"
            )
            await update.message.reply_text(reply, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"⚠️ Failed to create task via API.\nStatus: {response.status_code}\nError: {response.text}")

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
    
    print("Voice & Text Bot Started...")
    application.run_polling()

if __name__ == '__main__':
    main()
