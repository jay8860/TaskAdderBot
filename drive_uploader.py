from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import logging
import os
import json
import base64

SCOPES = ['https://www.googleapis.com/auth/drive.file']
SERVICE_ACCOUNT_FILE = 'credentials.json'


def _parse_google_credentials(raw: str):
    text = (raw or "").strip()
    if not text:
        return None
    # Accept plain JSON or base64-encoded JSON for safer env transport.
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        decoded = base64.b64decode(text).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None


def get_drive_service():
    creds = None
    
    # 1. Try Environment Variable (Best for Railway)
    json_content = os.getenv("GOOGLE_JSON") or os.getenv("GOOGLE_CREDENTIALS_JSON")
    if json_content:
        try:
            info = _parse_google_credentials(json_content)
            if not info:
                raise ValueError("Could not parse service account JSON from env.")
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            logging.error(f"Invalid Google credentials env: {e}")
            
    # 2. Try File (Best for Local)
    if not creds and os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    if not creds:
        logging.error("No valid Google Credentials found (File or Env).")
        return None
        
    return build('drive', 'v3', credentials=creds)

def upload_to_drive(file_path, original_name, mime_type):
    try:
        service = get_drive_service()
        if not service:
            return None

        file_metadata = {'name': original_name}
        folder_id = (os.getenv("GOOGLE_DRIVE_FOLDER_ID") or "").strip()
        if folder_id:
            file_metadata["parents"] = [folder_id]
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        logging.info(f"Uploading {original_name} to Drive...")
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        
        file_id = file.get('id')
        link = file.get('webViewLink') or (f"https://drive.google.com/file/d/{file_id}/view" if file_id else None)
        
        # Try public-read link; some org policies block this. If blocked, return internal link.
        if file_id:
            try:
                permission = {
                    'type': 'anyone',
                    'role': 'reader',
                }
                service.permissions().create(fileId=file_id, body=permission).execute()
            except Exception as perm_exc:
                logging.warning(f"Drive permission warning for file {file_id}: {perm_exc}")
        
        return link

    except Exception as e:
        logging.error(f"Drive Upload Error: {e}")
        return None
