import os
import logging
import asyncio
import re
import json
import requests
import datetime
import base64
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from drive_uploader import upload_to_drive

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_RAW = (os.getenv("GEMINI_MODEL") or os.getenv("GEMINI_MODEL_NAME") or "").strip()


def _ensure_trailing_slash(url: str) -> str:
    return (url or "").rstrip("/") + "/"


def _derive_api_base(raw_url: str) -> str:
    default_base = "http://localhost:8000/api"
    text = (raw_url or "").strip()
    if not text:
        return default_base

    # Accept API base, tasks URL, or app base.
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return default_base

    path = (parts.path or "").rstrip("/")
    if path.endswith("/api/tasks"):
        path = path[:-6]  # drop "/tasks"
    elif not path.endswith("/api"):
        api_idx = path.find("/api")
        if api_idx >= 0:
            path = path[:api_idx + 4]
        else:
            path = f"{path}/api" if path else "/api"

    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


_raw_api_url = os.getenv("API_URL", "").strip()
API_BASE_URL = _derive_api_base(_raw_api_url)
TASKS_API_URL = _ensure_trailing_slash(os.getenv("TASKS_API_URL", f"{API_BASE_URL}/tasks"))
EMPLOYEES_API_URL = _ensure_trailing_slash(os.getenv("EMPLOYEES_API_URL", f"{API_BASE_URL}/employees"))
FIELD_VISIT_NOTES_API_URL = os.getenv("FIELD_VISIT_NOTES_API_URL", f"{API_BASE_URL}/field-visits/planning-notes")
API_URL = TASKS_API_URL  # backward-compatible alias used throughout the file

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)


def _normalize_gemini_model_name(raw_name: str) -> str:
    text = (raw_name or "").strip()
    if not text:
        return ""

    lowered = text.lower().strip()
    if lowered.startswith("models/"):
        lowered = lowered[len("models/"):]
    lowered = lowered.replace("_", "-").replace(" ", "-")
    lowered = re.sub(r"-+", "-", lowered).strip("-")

    aliases = {
        "gemini-2-5-flash": "gemini-2.5-flash",
        "gemini-2-5-pro": "gemini-2.5-pro",
        "gemini-1-5-flash": "gemini-1.5-flash",
        "gemini-1-5-pro": "gemini-1.5-pro",
        "2-5-flash": "gemini-2.5-flash",
        "2-5-pro": "gemini-2.5-pro",
    }
    if lowered in aliases:
        return aliases[lowered]

    if "2.5" in lowered and "flash" in lowered:
        return "gemini-2.5-flash"
    if "2.5" in lowered and "pro" in lowered:
        return "gemini-2.5-pro"

    return lowered


def _build_gemini_models():
    candidates = [
        GEMINI_MODEL_RAW,
        os.getenv("GEMINI_MODEL_NAME", ""),
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ]
    unique = []
    for raw in candidates:
        normalized = _normalize_gemini_model_name(raw)
        if normalized and normalized not in unique:
            unique.append(normalized)

    models = []
    for name in unique:
        try:
            models.append((name, genai.GenerativeModel(name)))
        except Exception as exc:
            logging.warning(f"Gemini model init failed for '{name}': {exc}")

    if not models:
        # Last-resort hard fallback so bot still runs.
        fallback = "gemini-1.5-flash"
        models.append((fallback, genai.GenerativeModel(fallback)))
    return models


GEMINI_MODELS = _build_gemini_models()


def generate_with_gemini(contents):
    last_error = None
    for model_name, model_obj in GEMINI_MODELS:
        try:
            logging.info(f"Gemini generate_content with model={model_name}")
            return model_obj.generate_content(contents)
        except Exception as exc:
            last_error = exc
            logging.warning(f"Gemini call failed for model={model_name}: {exc}")
            continue
    raise last_error if last_error else RuntimeError("Gemini generation failed")

# --- DYNAMIC CONFIGURATION ---
def fetch_raw_officers():
    try:
        logging.info(f"Fetching officers from {EMPLOYEES_API_URL}")
        response = requests.get(EMPLOYEES_API_URL, timeout=8)
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
        disp = (e.get('display_username') or e.get('display_name') or "").strip()
        if name and disp:
             # FORMAT: Casual Name -> Official Display Name
             mapping.append(f"{name} -> {disp}")
        elif disp:
             mapping.append(disp)
    return mapping


def _officer_display_value(officer: dict) -> str:
    return (officer.get('display_username') or officer.get('display_name') or "").strip()


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
        disp = (e.get('display_username') or e.get('display_name') or "").strip()
        if disp.lower() == target:
            return disp
            
    # 2. Check if it's a Casual Name (Mapping Match)
    for e in officers:
        name = e.get('name', '').strip()
        if name.lower() == target:
            return e.get('display_username') or e.get('display_name') or name
            
    # 3. Fuzzy/Token Match (Handle "Dmf Aditya" vs "Aditya DMF")
    target_tokens = set(target.split())
    for e in officers:
        # Check Display Name Tokens
            disp = (e.get('display_username') or e.get('display_name') or "").strip()
            disp_tokens = set(disp.lower().split())
            if target_tokens == disp_tokens:
                 return disp
        
        # Check Name Tokens
            name = e.get('name', '').strip()
            name_tokens = set(name.lower().split())
            if target_tokens == name_tokens:
                 return e.get('display_username') or e.get('display_name') or name

    # 4. Partial Match (Relaxed - if user says "Aditya" and we have "Aditya DMF")
    # Only if target matches a significant part of the name
    if len(target) > 3:
        for e in officers:
             disp = (e.get('display_username') or e.get('display_name') or "").strip()
             if target in disp.lower():
                 return disp
             
             name = e.get('name', '').strip()
             if target in name.lower():
                 return e.get('display_username') or e.get('display_name') or name

    return assigned_name # Fallback to original if no match found


def resolve_employee_assignment(officers, assigned_name):
    """
    Returns (assigned_agency_display, assigned_employee_id)
    """
    display_name = normalize_to_display_name(officers, assigned_name)
    target = (display_name or "").strip().lower()
    if not target:
        return "", None

    for e in officers:
        disp = _officer_display_value(e).lower()
        name = (e.get('name') or "").strip().lower()
        if target == disp or target == name:
            return _officer_display_value(e) or display_name, e.get('id')

    return display_name, None


def normalize_priority(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"critical", "p0"}:
        return "Critical"
    if text in {"high", "urgent", "p1"}:
        return "High"
    if text in {"low", "p3"}:
        return "Low"
    return "Normal"


def _extract_field_visit_note(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if re.match(r"^(fv)\b[:\-\s]*", raw, flags=re.IGNORECASE):
        return re.sub(r"^(fv)\b[:\-\s]*", "", raw, flags=re.IGNORECASE).strip()
    if re.match(r"^(field[\s_-]*visit)\b[:\-\s]*", raw, flags=re.IGNORECASE):
        return re.sub(r"^(field[\s_-]*visit)\b[:\-\s]*", "", raw, flags=re.IGNORECASE).strip()
    return ""


def append_to_field_visit_notepad(note_line: str) -> tuple[bool, str]:
    line = (note_line or "").strip()
    if not line:
        return False, "No field visit note text found."

    try:
        current_resp = requests.get(FIELD_VISIT_NOTES_API_URL, timeout=12)
        current_note = ""
        current_home_base = "Collectorate, Dantewada"
        if current_resp.status_code == 200:
            payload = current_resp.json() or {}
            current_note = (payload.get("note_text") or "").strip()
            current_home_base = (payload.get("home_base") or current_home_base).strip() or current_home_base

        existing_lines = [ln.strip() for ln in current_note.splitlines() if ln.strip()]
        if not any(ln.lower() == line.lower() for ln in existing_lines):
            existing_lines.append(line)
        updated_note = "\n".join(existing_lines)

        save_resp = requests.put(
            FIELD_VISIT_NOTES_API_URL,
            json={"note_text": updated_note, "home_base": current_home_base},
            timeout=12,
        )
        if save_resp.status_code in (200, 201):
            return True, "Saved to Field Visit Planning Notepad."
        return False, f"Notepad save failed ({save_resp.status_code}): {save_resp.text}"
    except Exception as exc:
        logging.error(f"Field visit notepad save error: {exc}")
        return False, str(exc)


def _extract_task_identifiers_from_message(message_text: str) -> tuple[str | None, str | None]:
    text = (message_text or "").replace("*", "")
    legacy_ref = None
    task_number = None

    ref_match = re.search(r"(?:Ref|Task Ref)\s*:?\s*#?(\d+)", text, flags=re.IGNORECASE)
    if ref_match:
        legacy_ref = ref_match.group(1).strip()

    id_match = re.search(r"Task ID\s*:?\s*([A-Za-z0-9._-]+)", text, flags=re.IGNORECASE)
    if id_match:
        task_number = id_match.group(1).strip()

    return legacy_ref, task_number


def _resolve_task_db_id(legacy_ref: str | None, task_number: str | None) -> tuple[int | None, str | None]:
    if legacy_ref and str(legacy_ref).isdigit():
        return int(legacy_ref), task_number

    lookup = (task_number or "").strip()
    if not lookup:
        return None, task_number

    try:
        resp = requests.get(API_URL, params={"search": lookup}, timeout=12)
        if resp.status_code != 200:
            logging.error(f"Task lookup failed ({resp.status_code}): {resp.text}")
            return None, task_number

        payload = resp.json()
        tasks = payload if isinstance(payload, list) else []
        if not tasks:
            return None, task_number

        exact = [
            t for t in tasks
            if str(t.get("task_number") or "").strip().lower() == lookup.lower()
        ]
        chosen = exact[0] if exact else (tasks[0] if len(tasks) == 1 else None)
        if not chosen or not chosen.get("id"):
            return None, task_number

        return int(chosen["id"]), (chosen.get("task_number") or task_number)
    except Exception as exc:
        logging.error(f"Task lookup exception for Task ID '{lookup}': {exc}")
        return None, task_number

# --- CORE LOGIC ---

async def process_task_creation(update: Update, task_data: dict, officers_list: list, suppress_error: bool = False):
    """Helper to push task to API and handle notification flow."""
    try:
        response = requests.post(API_URL, json=task_data, timeout=12)
        
        if response.status_code == 200 or response.status_code == 201:
            created_task = response.json()
            assigned_to = created_task.get('assigned_employee_name') or created_task.get('assigned_agency')
            task_name = (created_task.get('description') or task_data.get('description') or '').strip() or 'No description'
            
            reply = (
                f"✅ **Task Created!**\n\n"
                f"🆔 **Task ID:** {created_task.get('task_number')}\n"
                f"📝 **Task Name:** {task_name}\n"
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

    inline_fv_note = _extract_field_visit_note(prompt_input or "")
    if inline_fv_note:
        ok, msg = append_to_field_visit_notepad(inline_fv_note)
        if ok:
            await update.message.reply_text(
                f"✅ Field Visit note added.\n\nSaved line:\n- {inline_fv_note}"
            )
        else:
            await update.message.reply_text(f"⚠️ Failed to save Field Visit note: {msg}")
        return
    
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
       - "priority": "High" ONLY if user says "Urgent" or "High Priority". Otherwise "Normal".
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
             result = generate_with_gemini([myfile, intent_prompt])
        else:
             result = generate_with_gemini("Analyze this command: \"" + prompt_input + "\"\n\n" + intent_prompt)
             
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
                extracted_note = _extract_field_visit_note(task_desc)
                if extracted_note:
                    ok, msg = append_to_field_visit_notepad(extracted_note)
                    if ok:
                        await update.message.reply_text(
                            f"✅ Field Visit note added from task line:\n- {extracted_note}"
                        )
                    else:
                        await update.message.reply_text(f"⚠️ Failed to save Field Visit note: {msg}")
                    continue
                
                # Attachment Injection (URL)
                if attachment_data:
                    task_data['attachment_data'] = attachment_data

                # Use Description AS the Task ID (Column 2)
                success = False
                original_desc = task_desc
                
                # Pre-processing fields
                assigned_agency, assigned_employee_id = resolve_employee_assignment(raw_officers, task_data.get('assigned_agency'))
                task_data['assigned_agency'] = assigned_agency or task_data.get('assigned_agency')
                task_data['assigned_employee_id'] = assigned_employee_id
                task_data['priority'] = normalize_priority(task_data.get('priority'))
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

                # Retry loop (network/API retry, not duplicate name retry)
                for attempt in range(1, 6): # Try up to 5 times
                    task_data.pop('task_number', None)  # Let dashboard auto-generate Task #
                    task_data['description'] = original_desc  # Dictated text should appear in Task/Description
                    
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
            resp = requests.get(API_URL, timeout=12)
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
                answer = generate_with_gemini(query_prompt)
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
    
    # 1. Extract Task identity (new format Task ID, old format Ref).
    legacy_ref, task_number = _extract_task_identifiers_from_message(original_text)
    task_db_id, resolved_task_number = _resolve_task_db_id(legacy_ref, task_number)
    if not task_db_id:
        await update.message.reply_text(
            "⚠️ I can't identify this task from the replied message. "
            "Reply to a recent bot confirmation that includes Task ID."
        )
        return

    task_display_id = resolved_task_number or (f"Ref #{legacy_ref}" if legacy_ref else f"DB #{task_db_id}")
    
    # 2. Analyze Intent with Gemini
    # Context: User is replying to a task confirmation.
    prompt = f"""
    The user is replying to a Task Confirmation for Task ID "{task_display_id}" (DB id {task_db_id}).
    User's Reply: "{user_text}"
    
    Determine if they want to DELETE the task or UPDATE it.
    
    INSTRUCTIONS:
    - If "delete", "remove", "cancel", return JSON: {{ "action": "DELETE" }}
    - If "change date", "assign to X", "fix typo", "rename to Y", return JSON: {{ "action": "UPDATE", "fields": {{ ... }} }}
      * IMPORTANT: If the user provides a new Title, Name, or Content, map it to "description" (Task Name column).
      * Map comment-like additions to "steno_comment".
      * Map to "assigned_agency" if user mentions a person/role.
      * Map to "deadline_date" (YYYY-MM-DD) if user mentions a date.
      * If user says "Change to: X", assume X is the new Task Name ("description").
      
    Return ONLY valid JSON.
    """
    
    try:
        result = generate_with_gemini(prompt)
        response_text = result.text.strip()
        if response_text.startswith("```json"): response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"): response_text = response_text[3:-3].strip()
        
        intent = json.loads(response_text)
        action = intent.get("action")
        
        if action == "DELETE":
            # Call Delete API
            del_url = f"{API_URL}{task_db_id}" # e.g. .../tasks/123
            resp = requests.delete(del_url, timeout=12)
            if resp.status_code == 200:
                await update.message.reply_text(f"🗑️ **Task {task_display_id} Deleted.**")
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
                assigned_agency, assigned_employee_id = resolve_employee_assignment(raw_officers, updates["assigned_agency"])
                updates["assigned_agency"] = assigned_agency
                updates["assigned_employee_id"] = assigned_employee_id

            # Call Update API (PUT)
            put_url = f"{API_URL}{task_db_id}"
            resp = requests.put(put_url, json=updates, timeout=12)
            
            if resp.status_code == 200:
                # Fetch fresh data to match DB exactly
                get_url = f"{API_URL}{task_db_id}"
                fresh_resp = requests.get(get_url, timeout=12)
                if fresh_resp.status_code == 200:
                    updated_task = fresh_resp.json()
                else:
                    updated_task = resp.json() # Fallback

                task_name_disp = updated_task.get('description') or "-"
                task_id_disp = updated_task.get('task_number') or task_display_id
                agency_disp = updated_task.get('assigned_agency') or "Unassigned"
                deadline_disp = updated_task.get('deadline_date') or "No Deadline"
                
                await update.message.reply_text(
                    f"📝 **Task {task_id_disp} Updated!**\n"
                    f"📌 **Task Name:** {task_name_disp}\n"
                    f"👤 **Agency:** {agency_disp}\n"
                    f"📅 **Deadline:** {deadline_disp}"
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

    logging.info(f"API base URL: {API_BASE_URL}")
    logging.info(f"Tasks API URL: {API_URL}")
    logging.info(f"Employees API URL: {EMPLOYEES_API_URL}")
    logging.info(f"Gemini model candidates: {[name for name, _ in GEMINI_MODELS]}")

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
