#requirements:
#google-auth
#requests
#wmill
#supabase
import wmill
import base64
import re
import requests as http_requests
from datetime import datetime
from google.oauth2 import service_account
from google.auth.transport.requests import Request as AuthRequest

def extract_gmail_data(gmail_message: dict) -> dict:
    def get_header(headers, name):
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

    headers = gmail_message['payload']['headers']
    subject = get_header(headers, 'Subject')
    wo_match = re.search(r'#(\d+)', subject) if subject else None
    wo_number = wo_match.group(1) if wo_match else None

    payload = gmail_message['payload']
    results = {'body_html': None, 'body_text': None}

    if payload.get('body', {}).get('data'):
        content = decode_part(payload)
        if payload.get('mimeType') == 'text/html':
            results['body_html'] = content
        elif payload.get('mimeType') == 'text/plain':
            results['body_text'] = content

    if payload.get('parts'):
        process_parts(payload['parts'], results)

    body_html = results['body_html']
    body_text = results['body_text']

    internal_date = datetime.fromtimestamp(int(gmail_message['internalDate']) / 1000)
    try:
        from email.utils import parsedate_to_datetime
        date_sent = parsedate_to_datetime(get_header(headers, 'Date')) if get_header(headers, 'Date') else internal_date
    except Exception:
        date_sent = internal_date

    return {
        'message_id': gmail_message['id'],
        'thread_id': gmail_message['threadId'],
        'subject': subject,
        'from_email': get_header(headers, 'From'),
        'to_email': get_header(headers, 'To'),
        'cc_emails': get_header(headers, 'Cc'),
        'message_id_header': get_header(headers, 'Message-ID'),
        'date_sent': date_sent.isoformat(),
        'wo_number': wo_number,
        'snippet': gmail_message['snippet'],
        'body_html': body_html,
        'body_text': body_text,
        'body_content': body_text or body_html or gmail_message['snippet'],
        'internal_date': internal_date.isoformat(),
        'is_unread': 'UNREAD' in gmail_message.get('labelIds', []),
        'references': get_header(headers, 'References'),
    }

def main(message_id_header: str):
    service_account_json = wmill.get_resource("u/carter/gmail_gcp_service_account")
    if 'private_key' in service_account_json:
        service_account_json['private_key'] = service_account_json['private_key'].replace('\\n', '\n')
    credentials = service_account.Credentials.from_service_account_info(
        service_account_json,
        scopes=['https://www.googleapis.com/auth/gmail.readonly']
    )
    delegated_credentials = credentials.with_subject('jpsbilling@jeffspoolspa.com')
    delegated_credentials.refresh(AuthRequest())
    headers = {'Authorization': f'Bearer {delegated_credentials.token}'}
    
    search_url = f'https://gmail.googleapis.com/gmail/v1/users/me/messages?q=rfc822msgid:{message_id_header}'
    search_response = http_requests.get(search_url, headers=headers)
    search_response.raise_for_status()
    messages = search_response.json().get('messages', [])
    
    if not messages:
        return {"emails": [], "count": 0}
    
    emails = []
    for msg in messages:
        msg_url = f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg["id"]}?format=full'
        msg_response = http_requests.get(msg_url, headers=headers)
        gmail_message = msg_response.json()
        email_data = extract_gmail_data(gmail_message)
        emails.append(email_data)
    
    return {"emails": emails, "count": len(emails)}