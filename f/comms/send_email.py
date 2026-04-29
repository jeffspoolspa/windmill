"""
Generic email sender via Gmail API with domain-wide delegation.
Logs activity via public.log_lead_activity RPC (avoids cross-schema PostgREST issue).
"""
# requirements:
# wmill
# google-auth
# google-auth-httplib2
# requests
# supabase
import base64
import wmill
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from supabase import create_client

AUTH_SENDER = "jpsbilling@jeffspoolspa.com"

OFFICE_BRANDING = {
    "richmond_hill": {"from_name": "Perfect Pools", "auto_cc": ["info@perfectpoolscleaning.com"]},
    "brunswick":     {"from_name": "Jeff's Pool & Spa Service", "auto_cc": []},
    "st_marys":      {"from_name": "Jeff's Pool & Spa Service", "auto_cc": []},
}


def _get_credentials():
    sa_info = wmill.get_resource("u/carter/gmail_gcp_service_account")
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=AUTH_SENDER,
    )
    creds.refresh(Request())
    return creds


def _attach_pdfs(msg, attachments):
    for att in attachments or []:
        filename = att.get("filename") or "attachment.pdf"
        mime_type = att.get("mime_type") or "application/pdf"
        content_b64 = att.get("content_base64")
        if not content_b64:
            continue
        maintype, _, subtype = mime_type.partition("/")
        part = MIMEBase(maintype or "application", subtype or "pdf")
        part.set_payload(base64.b64decode(content_b64))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)


def _log_activity(lead_id, result, html, text):
    try:
        url = wmill.get_variable("f/SUPABASE/URL")
        key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
        client = create_client(url, key)
        client.rpc("log_lead_activity", {
            "p_lead_id": lead_id,
            "p_activity_type": "email_sent",
            "p_description": (result.get("subject") or "")[:500],
            "p_metadata": {
                "message_id": result.get("message_id"),
                "thread_id": result.get("thread_id"),
                "to": result.get("to"),
                "cc": result.get("cc"),
                "bcc": result.get("bcc"),
                "from": result.get("from"),
                "reply_to": result.get("reply_to"),
                "office": result.get("office"),
                "subject": result.get("subject"),
                "body_html": html,
                "body_text": text,
                "attachment_count": result.get("attachment_count", 0),
            },
            "p_created_by": "system:send_email",
        }).execute()
    except Exception as e:
        print(f"[send_email] activity log failed (non-fatal): {e}")


def main(
    to: str,
    subject: str,
    html: str,
    office: str,
    lead_id: str,
    text: str = None,
    cc: list = None,
    bcc: list = None,
    attachments: list = None,
    from_name_override: str = None,
):
    if not lead_id: raise Exception("lead_id is required.")
    if not to: raise Exception("Recipient 'to' is required.")
    if not subject: raise Exception("Subject is required.")
    if not html: raise Exception("HTML body is required.")
    if office not in OFFICE_BRANDING:
        raise Exception(f"Unknown office '{office}'. Expected one of {list(OFFICE_BRANDING)}.")

    branding = OFFICE_BRANDING[office]
    from_name = from_name_override or branding["from_name"]

    merged_cc = []
    seen = {to.lower()}
    for addr in list(branding["auto_cc"]) + list(cc or []):
        if addr and addr.lower() not in seen:
            merged_cc.append(addr)
            seen.add(addr.lower())

    msg = MIMEMultipart("mixed")
    msg["To"] = to
    msg["From"] = f"{from_name} <{AUTH_SENDER}>"
    msg["Reply-To"] = AUTH_SENDER
    msg["Subject"] = subject
    if merged_cc: msg["Cc"] = ", ".join(merged_cc)
    if bcc: msg["Bcc"] = ", ".join(bcc)

    alt = MIMEMultipart("alternative")
    if text: alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    _attach_pdfs(msg, attachments)

    creds = _get_credentials()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        json={"raw": raw},
    )
    if not resp.ok:
        raise Exception(f"Gmail send failed ({resp.status_code}): {resp.text[:500]}")

    data = resp.json()
    result = {
        "success": True,
        "message_id": data.get("id"),
        "thread_id": data.get("threadId"),
        "to": to, "cc": merged_cc, "bcc": bcc or [],
        "from": f"{from_name} <{AUTH_SENDER}>",
        "reply_to": AUTH_SENDER,
        "office": office,
        "lead_id": lead_id,
        "subject": subject,
        "attachment_count": len(attachments or []),
    }
    _log_activity(lead_id, result, html, text or "")
    return result
