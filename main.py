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

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

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
        
        myfile = genai.upload_file(file_path)
        
        prompt = """
        Listen to this audio command. Extract the following details into a JSON object:
        - description: The full task description.
        - assigned_agency: The agency or person assigned (e.g., PWD, RES, Engineer Name). If not specified, null.
        - deadline_date: The deadline date in YYYY-MM-DD format. Calculate based on context (e.g., "next Friday"). If not specified, null.
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
        
        # 3. Create Task ID (Simple Random/Timestamp for now if API needs it, OR API handles it)
        # The API requires 'task_number'. We'll generate a "VOICE-XXX" number.
        task_data['task_number'] = f"V-{int(datetime.datetime.now().timestamp())}"
        task_data['source'] = "VoiceBot"
        
        # 4. Push to API
        response = requests.post(API_URL, json=task_data)
        
        # Clean up file
        os.remove(file_path)
        
        if response.status_code == 200 or response.status_code == 201:
            # Success
            created_task = response.json()
            reply = (
                f"✅ **Task Created Successfully!**\n\n"
                f"🆔 `{created_task.get('task_number')}`\n"
                f"📝 {created_task.get('description')}\n"
                f"👤 {created_task.get('assigned_agency') or 'Unassigned'}\n"
                f"📅 {created_task.get('deadline_date') or 'No Deadline'}"
            )
            await update.message.reply_text(reply, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"⚠️ Failed to create task via API.\nStatus: {response.status_code}\nError: {response.text}")

    except Exception as e:
        logging.error(f"Error processing voice: {e}")
        await update.message.reply_text("❌ Something went wrong while processing your voice note.")
        if os.path.exists(file_path):
            os.remove(file_path)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text_content = update.message.text
    logging.info(f"Received text from {user_id}: {text_content}")
    
    await update.message.reply_text("✍️ Processing your text...")

    try:
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        prompt = f"""
        You are a smart Task Extractor. Today is {today_str}.
        
        Analyze this command: "{text_content}"
        
        Extract the following details into a JSON object:
        - description: The full task description.
        - assigned_agency: The agency or person assigned (e.g., PWD, RES, Engineer Name). If not specified, null.
        - deadline_date: The deadline date in YYYY-MM-DD format. 
          * CRITICAL: If audio says "today", use {today_str}. 
          * If "tomorrow", use date+1. 
          * If "next week", use date+7. 
          * Do NOT default to a week if not specified. default to null.
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
        # We append a short random ID to ensure uniqueness in DB.
        import random
        short_id = str(random.randint(1000, 9999))
        
        # Main Task goes to task_number
        task_data['task_number'] = f"{task_data.get('description', 'Task')} ({short_id})"
        
        # Description field (Col 4) can be a fallback or same
        task_data['description'] = "Voice Entry"
        task_data['source'] = "VoiceBot"
        task_data['allocated_date'] = today_str
        
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
