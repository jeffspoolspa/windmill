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

    def decode_part(part):
        """Decode a Gmail part body using its declared charset, with fallbacks."""
        data = part.get('body', {}).get('data')
        if not data:
            return None
        raw = base64.urlsafe_b64decode(data)
        charset = None
        for h in part.get('headers') or []:
            if h.get('name', '').lower() == 'content-type':
                m = re.search(r'charset=\W*([\w.-]+)', h.get('value', ''), re.I)
                if m:
                    charset = m.group(1)
        for enc in [c for c in (charset, 'utf-8', 'cp1252', 'latin-1') if c]:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode('utf-8', errors='replace')

    def process_parts(parts, results):
        """Recursively collect HTML and plain text. Any HTML part is kept;
        an ION estimate table (<table...) wins if several HTML parts exist."""
        for part in parts:
            mime = part.get('mimeType', '')
            if mime == 'text/html':
                html = decode_part(part)
                if html and (not results['body_html'] or html.lstrip().startswith('<table')):
                    results['body_html'] = html
            elif mime == 'text/plain':
                text = decode_part(part)
                if text and not results['body_text']:
                    results['body_text'] = text
            elif mime.startswith('multipart/') and part.get('parts'):
                process_parts(part['parts'], results)

    # Extract headers
    headers = gmail_message['payload']['headers']
    subject = get_header(headers, 'Subject')
    from_email = get_header(headers, 'From')
    to_email = get_header(headers, 'To')
    date = get_header(headers, 'Date')
    message_id_header = get_header(headers, 'Message-ID')
    cc_email = get_header(headers, 'Cc')
    references = get_header(headers, 'References')

    # Extract WO number from subject
    wo_match = re.search(r'#(\d+)', subject) if subject else None
    wo_number = wo_match.group(1) if wo_match else None

    # Extract body content
    payload = gmail_message['payload']
    results = {'body_html': None, 'body_text': None}

    # Simple email (body directly in payload.body)
    if payload.get('body', {}).get('data'):
        content = decode_part(payload)
        if payload.get('mimeType') == 'text/html':
            results['body_html'] = content
        elif payload.get('mimeType') == 'text/plain':
            results['body_text'] = content

    # Multipart emails (body in payload.parts)
    if payload.get('parts'):
        process_parts(payload['parts'], results)

    body_html = results['body_html']
    body_text = results['body_text']

    # Convert internal date from milliseconds to datetime
    internal_date = datetime.fromtimestamp(int(gmail_message['internalDate']) / 1000)

    # Parse date header (optional, could use internal_date instead)
    try:
        from email.utils import parsedate_to_datetime
        date_sent = parsedate_to_datetime(date) if date else internal_date
    except Exception:
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
            headers={'Authorization': f'Bearer {token}'},
            params={'format': 'full'}
        )
        details.append(msg_response.json())

    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")

    supabase = create_client(url, key)

    summary = []
    for email in details:

        # Extract data from Gmail message
        email_data = extract_gmail_data(email)

        try:
            # Insert into est_emails table (upsert to handle duplicates)
            response = supabase.table('est_emails').upsert(
                email_data,
                on_conflict='message_id'
            ).execute()

            summary.append({
                "success": True,
                "message_id": email_data['message_id'],
                "from": email_data['from_email'],
                "has_html": email_data['body_html'] is not None,
                "has_text": email_data['body_text'] is not None,
            })

        except Exception as e:
            summary.append({
                "success": False,
                "error": str(e),
                "message_id": email_data.get('message_id')
            })

    print(summary)
    return summary