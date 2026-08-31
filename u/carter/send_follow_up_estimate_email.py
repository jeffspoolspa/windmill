#requirements:
#google-auth
#requests
#wmill
#supabase

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from html import unescape
import re
import requests
import base64
import wmill
from google.oauth2 import service_account
from google.auth.transport.requests import Request as AuthRequest
from supabase import create_client
from datetime import date

FOLLOWUP_LIMIT = 3


def html_to_text(html: str) -> str:
    """Cheap HTML -> plain text for the text/plain alternative."""
    text = re.sub(r'(?i)<br\s*/?>', '\n', html)
    text = re.sub(r'(?i)</(p|div|tr|li|h\d)>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def count_followups(rows: list) -> int:
    """Outbound emails from the billing mailbox, excluding ION-relayed originals."""
    n = 0
    for r in rows:
        f = (r.get('from_email') or '').lower()
        if 'jpsbilling@jeffspoolspa.com' in f and 'donotreply@ionpoolcare.com' not in f:
            n += 1
    return n


def main(email: str, wo_number: str, office: str, message: str, cc: list, attach_pdf: bool, force: bool = False):
    service_account_info = wmill.get_resource("u/carter/gmail_gcp_service_account")

    if 'private_key' in service_account_info:
        service_account_info['private_key'] = service_account_info['private_key'].replace('\\n', '\n')

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=['https://www.googleapis.com/auth/gmail.send']
    )
    delegated_credentials = credentials.with_subject('jpsbilling@jeffspoolspa.com')
    delegated_credentials.refresh(AuthRequest())
    token = delegated_credentials.token

    supabase_url = wmill.get_variable("f/SUPABASE/URL")
    supabase_key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(supabase_url, supabase_key)

    result = (
        supabase.table('est_emails')
        .select("*")
        .eq('wo_number', wo_number)
        .order('created_at', desc=False)
        .execute()
    )

    if not result.data:
        raise ValueError(f"No email found for wo_number: {wo_number}")

    already_sent = count_followups(result.data)
    if already_sent >= FOLLOWUP_LIMIT and not force:
        print(f"Skipping WO #{wo_number}: {already_sent} follow-ups already sent (limit {FOLLOWUP_LIMIT})")
        return {
            "status": "skipped",
            "reason": f"{already_sent} follow-ups already sent (limit {FOLLOWUP_LIMIT})",
            "wo_number": wo_number,
            "followups_sent": already_sent,
        }

    last = result.data[-1]
    last_msg_id = last['message_id_header']
    subject = last['subject']
    refs = last['references']
    thread_id = last['thread_id']
    if office == "Richmond Hill":
        cc.append("info@perfectpoolscleaning.com")
    cc_emails = ", ".join(cc)
    print(subject)

    # Outer container: mixed (body + optional attachment)
    msg = MIMEMultipart('mixed')
    msg['To'] = email
    msg['Cc'] = cc_emails
    msg['Subject'] = subject
    msg['In-Reply-To'] = last_msg_id
    msg['References'] = f"{refs} {last_msg_id}" if refs else last_msg_id

    # Body: alternative (plain first, html preferred by clients)
    body = MIMEMultipart('alternative')
    body.attach(MIMEText(html_to_text(message), 'plain', 'utf-8'))
    body.attach(MIMEText(message, 'html', 'utf-8'))
    msg.attach(body)

    if attach_pdf:
        pdf_path = f"{wo_number}.pdf"
        pdf_data = supabase.storage.from_('estimates').download(pdf_path)
        pdf_attachment = MIMEApplication(pdf_data, _subtype='pdf')
        pdf_attachment.add_header('Content-Disposition', 'attachment', filename=f'{wo_number}.pdf')
        msg.attach(pdf_attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')

    response = requests.post(
        'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
        headers={'Authorization': f'Bearer {token}'},
        json={'raw': raw, 'threadId': thread_id}
    )

    response.raise_for_status()
    update = (
        supabase.table('estimates')
        .update({'last_sent': date.today().isoformat()})
        .eq('wo_number', wo_number)
        .execute()
    )

    return {"status": "sent", "wo_number": wo_number, "followups_sent": already_sent + 1}