"""
Sends pending rows from public.system_alerts as emails via Gmail API.

- Polls every cron tick (default cadence: every 5 minutes).
- Uses domain-wide-delegated Gmail service account (same one f/comms/send_email uses).
- Marks sent rows status='sent', sent_at=now(). On send failure, increments send_attempts and records last_send_error; after 5 failures, marks status='failed'.
- Sends from "Jeff's Pool & Spa Service Alerts <jpsbilling@jeffspoolspa.com>".
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
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from supabase import create_client

AUTH_SENDER = "jpsbilling@jeffspoolspa.com"
FROM_NAME = "Jeff's Pool & Spa Service Alerts"
MAX_ATTEMPTS = 5
BATCH_SIZE = 20


def _get_supabase():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
    return create_client(url, key)


def _get_gmail_creds():
    sa_info = wmill.get_resource("u/carter/gmail_gcp_service_account")
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=AUTH_SENDER,
    )
    creds.refresh(Request())
    return creds


def _send_one(creds, to, subject, html, text):
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = f"{FROM_NAME} <{AUTH_SENDER}>"
    msg["Reply-To"] = AUTH_SENDER
    msg["Subject"] = subject
    if text:
        msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        json={"raw": raw},
        timeout=30,
    )
    if not resp.ok:
        raise Exception(f"Gmail send failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def main():
    sb = _get_supabase()

    rows = (
        sb.table("system_alerts")
          .select("id, recipient, subject, body_html, body_text, send_attempts")
          .eq("status", "pending")
          .lt("send_attempts", MAX_ATTEMPTS)
          .order("created_at")
          .limit(BATCH_SIZE)
          .execute()
          .data
    )

    if not rows:
        return {"sent": 0, "skipped": 0, "failed": 0, "message": "no pending alerts"}

    creds = _get_gmail_creds()

    sent = 0
    failed = 0
    errors = []

    for r in rows:
        alert_id = r["id"]
        try:
            result = _send_one(
                creds,
                to=r["recipient"],
                subject=r["subject"],
                html=r["body_html"],
                text=r.get("body_text") or "",
            )
            sb.table("system_alerts").update({
                "status": "sent",
                "sent_at": "now()",
                "send_attempts": (r.get("send_attempts") or 0) + 1,
                "last_send_error": None,
            }).eq("id", alert_id).execute()
            sent += 1
        except Exception as e:
            msg = str(e)[:1000]
            attempts = (r.get("send_attempts") or 0) + 1
            new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            sb.table("system_alerts").update({
                "status": new_status,
                "send_attempts": attempts,
                "last_send_error": msg,
            }).eq("id", alert_id).execute()
            failed += 1
            errors.append({"id": alert_id, "error": msg, "attempts": attempts, "status": new_status})

    return {"sent": sent, "failed": failed, "errors": errors}
