#requirements:
#requests
#google-auth
#wmill
#psycopg2-binary

import requests
import wmill
import base64
import json
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import psycopg2
from email.utils import parsedate_to_datetime


def get_gmail_token(impersonate_email: str) -> str:
    sa_info = wmill.get_resource("u/carter/gmail_gcp_service_account")
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/gmail.readonly'],
        subject=impersonate_email
    )
    credentials.refresh(Request())
    return credentials.token


def get_supabase_conn():
    db = wmill.get_resource("u/carter/supabase")
    return psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"],
        sslmode=db.get("sslmode", "require")
    )


def search_messages(token: str, query: str) -> list:
    messages = []
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": query, "maxResults": 100}
    while True:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        messages.extend(data.get("messages", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token
    return messages


def get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def find_pdf_attachments(payload: dict) -> list:
    attachments = []
    if payload.get("mimeType") == "application/pdf" and payload.get("body", {}).get("attachmentId"):
        attachments.append({
            "attachment_id": payload["body"]["attachmentId"],
            "filename": payload.get("filename", "unknown.pdf"),
        })
    for part in payload.get("parts", []):
        attachments.extend(find_pdf_attachments(part))
    return attachments


def main(
    project_name: str,
    gmail_query: str,
    impersonate_email: str = "jpsbilling@jeffspoolspa.com"
):
    """
    Download PDF attachments from Gmail and store in Supabase.
    
    Args:
        project_name: e.g. 'allied_universal_2025'
        gmail_query: e.g. 'from:no-reply@allieduniversal.com after:2024/12/31 before:2026/01/01 has:attachment'
        impersonate_email: Email to impersonate via service account
    """
    token = get_gmail_token(impersonate_email)
    print(f"Authenticated as {impersonate_email}")

    messages = search_messages(token, gmail_query)
    print(f"Found {len(messages)} matching emails")

    if not messages:
        return {"emails_found": 0, "pdfs_downloaded": 0, "skipped": 0, "errors": []}

    conn = get_supabase_conn()
    cur = conn.cursor()

    downloaded = 0
    skipped = 0
    errors = []

    for msg in messages:
        msg_id = msg["id"]
        try:
            # Skip if already downloaded
            cur.execute("SELECT 1 FROM email_extraction.email_attachments WHERE gmail_message_id = %s", (msg_id,))
            if cur.fetchone():
                skipped += 1
                continue

            # Get message detail
            detail = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                headers={"Authorization": f"Bearer {token}"}
            ).json()

            headers = detail["payload"]["headers"]
            subject = get_header(headers, "Subject")
            from_email = get_header(headers, "From")
            date_str = get_header(headers, "Date")
            date_sent = None
            try:
                date_sent = parsedate_to_datetime(date_str) if date_str else None
            except:
                pass

            pdf_parts = find_pdf_attachments(detail["payload"])
            if not pdf_parts:
                continue

            for pdf_part in pdf_parts:
                # Download attachment
                att_resp = requests.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/attachments/{pdf_part['attachment_id']}",
                    headers={"Authorization": f"Bearer {token}"}
                )
                att_resp.raise_for_status()
                pdf_bytes = base64.urlsafe_b64decode(att_resp.json()["data"])
                pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

                cur.execute("""
                    INSERT INTO email_extraction.email_attachments 
                    (project_name, gmail_message_id, subject, from_email, date_sent, 
                     filename, mime_type, pdf_base64, extraction_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (gmail_message_id) DO NOTHING
                """, (project_name, msg_id, subject, from_email, date_sent,
                      pdf_part["filename"], "application/pdf", pdf_b64))
                conn.commit()
                downloaded += 1
                print(f"  Downloaded: {pdf_part['filename']}")

        except Exception as e:
            errors.append({"message_id": msg_id, "error": str(e)})
            print(f"  ERROR on {msg_id}: {e}")
            conn.rollback()

    cur.close()
    conn.close()

    result = {"emails_found": len(messages), "pdfs_downloaded": downloaded, "skipped": skipped, "errors": errors}
    print(f"\nComplete: {json.dumps(result, indent=2)}")
    return result
