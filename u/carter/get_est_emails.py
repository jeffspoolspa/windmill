#requirements:
#requests
#google-auth
#wmill
# supabase==2.8.1
import requests
import wmill
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import base64
import re
from datetime import datetime
from supabase import create_client

def extract_gmail_data(gmail_message: dict) -> dict:
    """
    Extracts and formats Gmail API message data for database insertion
    """
    
    def get_header(headers, name):
        """Find header value by name (case-insensitive)"""
        header = next((h for h in headers if h['name'].lower() == name.lower()), None)
        return header['value'] if header else None
    
    def decode_base64(data):
        """Decode URL-safe base64 content"""
        if not data:
            return None
        try:
            return base64.urlsafe_b64decode(data).decode('utf-8')
        except Exception as e:
            print(f"Base64 decode error: {e}")
            return None
    
    def process_parts(parts, results):
        """Recursively process email parts to extract HTML and plain text"""
        for part in parts:
            if part.get('mimeType') == 'text/html' and part.get('body', {}).get('data'):
                html = decode_base64(part['body']['data'])
                if html and html.startswith("<table"):
                    results['body_html'] = html
            elif part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                results['body_text'] = decode_base64(part['body']['data'])
            elif part.get('mimeType', '').startswith('multipart/') and part.get('parts'):
                process_parts(part['parts'], results)
    
    # Extract headers
    headers = gmail_message['payload']['headers']
    subject = get_header(headers, 'Subject')
    from_email = get_header(headers, 'From')
    to_email = get_header(headers, 'To')
    date = get_header(headers, 'Date')
    message_id_header = get_header(headers, 'Message-ID')
    cc_email = get_header(headers, 'Cc')
    references = get_header(headers, 'References'),
    
    # Extract WO number from subject
    wo_match = re.search(r'#(\d+)', subject) if subject else None
    wo_number = wo_match.group(1) if wo_match else None
    
    # Extract body content
    body_html = None
    body_text = None
    
    # Check if simple email (body directly in payload.body)
    if gmail_message['payload'].get('body', {}).get('data'):
        content = decode_base64(gmail_message['payload']['body']['data'])
        
        if gmail_message['payload'].get('mimeType') == 'text/html':
            body_html = content
        elif gmail_message['payload'].get('mimeType') == 'text/plain':
            body_text = content
    
    # Check multipart emails (body in payload.parts)
    if gmail_message['payload'].get('parts'):
        results = {'body_html': None, 'body_text': None}
        process_parts(gmail_message['payload']['parts'], results)
        body_html = body_html or results['body_html']
        body_text = body_text or results['body_text']
    
    # Convert internal date from milliseconds to datetime
    internal_date = datetime.fromtimestamp(int(gmail_message['internalDate']) / 1000)
    
    # Parse date header (optional, could use internal_date instead)
    try:
        from email.utils import parsedate_to_datetime
        date_sent = parsedate_to_datetime(date) if date else internal_date
    except:
        date_sent = internal_date
    
    # Return formatted data for database
    return {
        'message_id': gmail_message['id'],
        'thread_id': gmail_message['threadId'],
        'subject': subject,
        'from_email': from_email,
        'to_email': to_email,
        'cc_emails': cc_email,
        'message_id_header': message_id_header,
        'date_sent': date_sent.isoformat(),
        'wo_number': wo_number,
        'snippet': gmail_message['snippet'],
        'body_html': body_html,
        'body_text': body_text,
        'body_content': body_text or body_html or gmail_message['snippet'],
        'internal_date': internal_date.isoformat(),
        'is_unread': 'UNREAD' in gmail_message.get('labelIds', []),
        'references': references

    }

def main(wo_number: str, user_email: str):
    # Get service account credentials

    service_account_info = wmill.get_resource("u/carter/gmail_gcp_service_account")

    subject = f"Jeff's Pool and Spa Service - Work Order Estimate #{wo_number}"
    
    # Create credentials with domain-wide delegation
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=['https://www.googleapis.com/auth/gmail.readonly'],
        subject=user_email  # Impersonate this user
    )
    
    # Get access token
    credentials.refresh(Request())
    token = credentials.token
    
    # Search for messages
    response = requests.get(
        'https://gmail.googleapis.com/gmail/v1/users/me/messages',
        headers={'Authorization': f'Bearer {token}'},
        params={'q': f'subject:"{subject}"'}
    )
    
    response.raise_for_status()
    messages = response.json().get('messages', [])
    
    print(f"Found {len(messages)} messages")
    
    # Get full details for each message
    details = []
    for msg in messages:
        msg_response = requests.get(
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg["id"]}',
            headers={'Authorization': f'Bearer {token}'}
        )
        details.append(msg_response.json())
    
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")

    supabase = create_client(url, key)

    for email in details:
        
        # Extract data from Gmail message
        email_data = extract_gmail_data(email)
        
        try:
            # Insert into est_emails table (upsert to handle duplicates)
            response = supabase.table('est_emails').upsert(
                email_data,
                on_conflict='message_id'
            ).execute()
            
            print ({
                "success": True,
                "inserted": len(response.data) > 0,
                "data": response.data[0] if response.data else None,
                "wo_number": email_data['wo_number'],
                "message_id": email_data['message_id']
            })
                
        except Exception as e:
            print ({
                "success": False,
                "error": str(e),
                "message_id": email_data.get('message_id')
            })

    return details