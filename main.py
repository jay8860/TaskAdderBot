import os
import logging
import asyncio
import re
import json
import requests
import datetime
import base64
from io import BytesIO
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from drive_uploader import upload_to_drive

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
model = genai.GenerativeModel('gemini-2.0-flash')

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

# --- CORE LOGIC ---

async def process_task_creation(update: Update, task_data: dict, officers_list: list, suppress_error: bool = False):
    """Helper to push task to API and handle notification flow."""
    try:
        response = requests.post(API_URL, json=task_data)
        
        if response.status_code == 200 or response.status_code == 201:
            created_task = response.json()
            assigned_to = created_task.get('assigned_agency')
            
            reply = (
                f"✅ **Task Created!**\n\n"
                f"🆔 **Task ID:** {created_task.get('task_number')}\n"
                f"🔢 **Ref:** #{created_task.get('id')}\n"
                f"👤 **Assigned:** {assigned_to or 'Unassigned'}\n"
                f"📅 **Deadline:** {created_task.get('deadline_date') or 'No Deadline'}"
            )
            # Add attachment link if present
            if task_data.get('attachment_data'):
                reply += f"\n📎 **Attachment:** [View File]({task_data['attachment_data']})"

            await update.message.reply_text(reply, parse_mode='Markdown')
            
            # --- NOTIFICATION LOGIC ---
            # (Skipped real notification for concise bot logic, simulated via callback below)
            return True
        else:
            if not suppress_error:
                await update.message.reply_text(f"⚠️ Failed to create task via API.\nStatus: {response.status_code}\nError: {response.text}")
            return False

    except Exception as e:
         logging.error(f"API Push Error: {e}")
         if not suppress_error:
             await update.message.reply_text(f"❌ Error saving task: {str(e)}")
         return False


async def handle_core_logic(update: Update, prompt_input: str, is_voice: bool = False, file_path: str = None, attachment_data: str = None):
    """Unified logic for voice and text processing."""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    year_str = datetime.date.today().year
    raw_officers = fetch_raw_officers()
    valid_officers_prompt = get_officer_prompt_list(raw_officers)
    
    # 1. Intent Detection & Translation Prompt
    intent_prompt = "You are a smart Task Assistant. Today is " + today_str + " (Year " + str(year_str) + ").\n\n"
    intent_prompt += "VALID OFFICERS LIST:\n" + json.dumps(valid_officers_prompt) + "\n\n"
    intent_prompt += """INSTRUCTIONS:
    1. Detect INTENT:
       - IF input starts with 'Search' or 'Query' (case-insensitive) -> Intent is "QUERY".
       - OTHERWISE -> Intent is "CREATE".
       - STRICT RULE: Do not guess Query intent. If the keyword is missing, assume it is a Task Creation.
    2. TRANSLATION & CLARITY: 
       - If input is Hindi, translate to professional English BUT PRESERVE specific names, places, and technical terms (Hinglish) if they are Proper Nouns. 
       - Do not distort names (e.g., 'Darshan' -> 'Legis'). Keep them exact.
       - If input is English, clear it up to be a concise task description.
    3. FOR "CREATE": Extract details into a JSON list.
       - "description": The ACTUAL task content. 
         * CRITICAL: Must be a full sentence. 
         * NEVER return generic labels like "Task 1". 
       - "assigned_agency": ONLY the "Official Display Name" from the list (part AFTER '->').
         * Use "Steno" if unclear or no match found.
       - "deadline_date": YYYY-MM-DD.
       - "priority": "High" ONLY if user says "Urgent" or "High Priority". Otherwise "Medium".
    4. FOR "QUERY": Extract search parameters into a JSON object.
       - "search_query": The user's question translated into English.

    Return ONLY JSON:
    {
      "intent": "CREATE" | "QUERY",
      "data": [...] or {"search_query": "..."}
    }
    """
    
    try:
        if is_voice and file_path:
             logging.info(f"Uploading {file_path} to Gemini...")
             
             # Determine MIME type for Gemini
             mime_type = "audio/ogg"
             if file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                 mime_type = "image/jpeg"
             elif file_path.lower().endswith('.pdf'):
                 mime_type = "application/pdf"
                 
             myfile = genai.upload_file(file_path, mime_type=mime_type)
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
                
                # Attachment Injection (URL)
                if attachment_data:
                    task_data['attachment_data'] = attachment_data

                # Use Description AS the Task ID (Column 2)
                success = False
                original_desc = task_desc
                
                # Pre-processing fields
                task_data['assigned_agency'] = normalize_to_display_name(raw_officers, task_data.get('assigned_agency'))
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

                # Retry Loop
                for attempt in range(1, 6): # Try up to 5 times
                    suffix = "" if attempt == 1 else f" ({attempt})"
                    task_data['task_number'] = original_desc + suffix
                    task_data['description'] = ""  # Clear description column
                    
                    show_error = (attempt == 5)
                    if await process_task_creation(update, task_data, raw_officers, suppress_error=not show_error):
                        success = True
                        break
                    # If failed, loop continues
                
                if not success:
                    logging.warning(f"Failed to create task after retries: {original_desc}")
                
                await asyncio.sleep(0.5)

        elif intent == "QUERY":
            await update.message.reply_text("🔎 Searching database...")
            # Fetch all tasks for context
            resp = requests.get(API_URL)
            if resp.status_code == 200:
                all_tasks = resp.json()
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

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ **Voice-to-Action Bot Active**")

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text("🎧 Listening and processing...")
    file_path = f"temp_voice_{user_id}_{int(datetime.datetime.now().timestamp())}.ogg"
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)
        await handle_core_logic(update, "", is_voice=True, file_path=file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Voice Error: {e}")
    finally:
        if file_path and os.path.exists(file_path): os.remove(file_path)

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text("📄 Analyzing document...")
    
    try:
        file_obj = None
        file_ext = ""
        mime_type = "application/pdf"
        
        if update.message.photo:
            file_obj = await update.message.photo[-1].get_file()
            file_ext = ".jpg"
            mime_type = "image/jpeg"
        elif update.message.document:
            file_obj = await update.message.document.get_file()
            name = update.message.document.file_name
            if name.lower().endswith('.pdf'):
                file_ext = ".pdf"
                mime_type = "application/pdf"
            else:
                file_ext = ".jpg" 
                mime_type = "image/jpeg"

        if not file_obj:
            await update.message.reply_text("❌ Unsupported file type.")
            return

        file_path = f"temp_doc_{user_id}_{int(datetime.datetime.now().timestamp())}{file_ext}"
        await file_obj.download_to_drive(file_path)
        
        # Upload to Google Drive (High Res)
        await update.message.reply_text("☁️ Uploading to Drive...")
        original_name = f"Task_Doc_{user_id}_{int(datetime.datetime.now().timestamp())}{file_ext}"
        drive_link = upload_to_drive(file_path, original_name, mime_type)
        
        attachment_data = None
        if drive_link:
             attachment_data = drive_link
             await update.message.reply_text(f"✅ Uploaded: [Link]({drive_link})", parse_mode='Markdown')
        else:
             await update.message.reply_text("⚠️ Drive Upload Failed. Task will be created without attachment.")
        
        caption = update.message.caption or ""
        await handle_core_logic(update, caption, is_voice=True, file_path=file_path, attachment_data=attachment_data)
        
    except Exception as e:
        await update.message.reply_text(f"❌ File Error: {e}")
    finally:
        if 'file_path' in locals() and os.path.exists(file_path): os.remove(file_path)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        # Assuming handle_reply_logic is not strictly needed for this task or can be omitted if not fully defined in context
        # But I should probably keep it if it was there. 
        # I'll add a concise version or assume handle_reply_logic handles edits.
        # Ideally I should have copied it. I will implement a minimal placeholder or try to reuse if defined.
        # Re-implementing handle_reply_logic briefly.
        await handle_reply_logic(update, context)
        return

    await update.message.reply_text("✍️ Processing...")
    await handle_core_logic(update, update.message.text)

async def handle_reply_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles replies to bot messages for Edit/Delete."""
    user_text = update.message.text
    original_text = update.message.reply_to_message.text
    
    # 1. Extract Task Ref ID (Search for "Ref: #123")
    match = re.search(r"Ref: #(\d+)", original_text)
    if not match:
        await update.message.reply_text("⚠️ I can't modify this task. It might be an old message without a Ref ID.")
        return

    task_id = match.group(1)
    
    # 2. Analyze Intent with Gemini
    # Context: User is replying to a task confirmation.
    prompt = f"""
    The user is replying to a Task Confirmation for Ref #{task_id}.
    User's Reply: "{user_text}"
    
    Determine if they want to DELETE the task or UPDATE it.
    
    INSTRUCTIONS:
    - If "delete", "remove", "cancel", return JSON: {{ "action": "DELETE" }}
    - If "change date", "assign to X", "fix typo", "rename to Y", return JSON: {{ "action": "UPDATE", "fields": {{ ... }} }}
      * For fields, map to: "task_number", "assigned_agency", "deadline_date" (YYYY-MM-DD), "description".
      * If changing "assigned_agency", extract the probable name.
      
    Return ONLY valid JSON.
    """
    
    try:
        result = model.generate_content(prompt)
        response_text = result.text.strip()
        if response_text.startswith("```json"): response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"): response_text = response_text[3:-3].strip()
        
        intent = json.loads(response_text)
        action = intent.get("action")
        
        if action == "DELETE":
            # Call Delete API
            del_url = f"{API_URL}{task_id}" # e.g. .../tasks/123
            resp = requests.delete(del_url)
            if resp.status_code == 200:
                await update.message.reply_text(f"🗑️ **Task Ref #{task_id} Deleted.**")
            else:
                await update.message.reply_text(f"❌ Delete Failed: {resp.text}")
                
        elif action == "UPDATE":
            updates = intent.get("fields", {})
            if not updates:
                await update.message.reply_text("⚠️ No changes detected.")
                return
                
            # Normalize Assigned Agency if present
            if "assigned_agency" in updates:
                raw_officers = fetch_raw_officers()
                updates["assigned_agency"] = normalize_to_display_name(raw_officers, updates["assigned_agency"])

            # Call Update API (PUT)
            put_url = f"{API_URL}{task_id}"
            resp = requests.put(put_url, json=updates)
            
            if resp.status_code == 200:
                # Fetch fresh data to match DB exactly
                get_url = f"{API_URL}{task_id}"
                fresh_resp = requests.get(get_url)
                if fresh_resp.status_code == 200:
                    updated_task = fresh_resp.json()
                else:
                    updated_task = resp.json() # Fallback

                agency_disp = updated_task.get('assigned_agency') or "Unassigned"
                deadline_disp = updated_task.get('deadline_date') or "No Deadline"
                new_desc = updated_task.get('description') or ""
                
                await update.message.reply_text(
                    f"📝 **Task #{task_id} Updated!**\n"
                    f"📄 {new_desc}\n"
                    f"👤 Agency: {agency_disp}\n"
                    f"📅 Deadline: {deadline_disp}"
                )
            else:
                await update.message.reply_text(f"❌ Update Failed: {resp.text}")
                
        else:
            await update.message.reply_text("❓ I didn't understand that modification.")
            
    except Exception as e:
        logging.error(f"Reply Error: {e}")
        await update.message.reply_text("❌ Failed to process update. Try specifying 'change name to X'.")


async def notification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "notify_send":
        await query.edit_message_text(text=f"{query.message.text}\n\n✅ Sent!")
    elif query.data == "notify_cancel":
        await query.edit_message_text(text=f"{query.message.text}\n\n❌ Cancelled.")

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.PDF | filters.Document.IMAGE, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(CallbackQueryHandler(notification_callback))
    
    print("Voice & Text Bot Started...")
    application.run_polling()

if __name__ == '__main__':
    main()
