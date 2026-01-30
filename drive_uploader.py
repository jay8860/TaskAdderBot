from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import logging
import os

SCOPES = ['https://www.googleapis.com/auth/drive.file']
SERVICE_ACCOUNT_FILE = 'credentials.json'

def get_drive_service():
    creds = None
    
    # 1. Try Environment Variable (Best for Railway)
    json_content = os.getenv("GOOGLE_JSON")
    if json_content:
        try:
            info = json.loads(json_content)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            logging.error(f"Invalid GOOGLE_JSON env: {e}")
            
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
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        logging.info(f"Uploading {original_name} to Drive...")
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        
        file_id = file.get('id')
        link = file.get('webViewLink')
        
        # Make Publicly Readable
        permission = {
            'type': 'anyone',
            'role': 'reader',
        }
        service.permissions().create(fileId=file_id, body=permission).execute()
        
        return link

    except Exception as e:
        logging.error(f"Drive Upload Error: {e}")
        return None
