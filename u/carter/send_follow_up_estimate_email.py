#requirements:
#google-auth
#requests
#wmill
#supabase

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import requests
import base64
import wmill
from google.oauth2 import service_account
from google.auth.transport.requests import Request as AuthRequest
from supabase import create_client
from datetime import date

def main(email: str, wo_number: str, office: str, message: str, cc: list, attach_pdf: bool):
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

    if attach_pdf:
        pdf_path = f"{wo_number}.pdf"
        pdf_data = supabase.storage.from_('estimates').download(pdf_path)

    result = (
        supabase.table('est_emails')
        .select("*")
        .eq('wo_number', wo_number)
        .order('created_at', desc=False)
        .execute()
    )

    if not result.data:
        raise ValueError(f"No email found for wo_number: {wo_number}")

    last = result.data[-1]
    last_msg_id = last['message_id_header']
    subject = last['subject']
    refs = last['references']
    thread_id = last['thread_id']
    if office == "Richmond Hill":
        cc.append("info@perfectpoolscleaning.com")
    cc_emails = ", ".join(cc)
    print(subject)

    msg = MIMEMultipart()
    msg['To'] = email
    msg['Cc'] = cc_emails
    msg['Subject'] = subject
    msg['In-Reply-To'] = last_msg_id
    msg['References'] = f"{refs} {last_msg_id}"

    msg.attach(MIMEText(message, 'html'))

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

    return {"status": "sent"}