
import requests
import psycopg2
import wmill
import json
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from datetime import datetime


CARD_TYPE_DISPLAY = {
    "VISA": "Visa",
    "MC": "Mastercard",
    "AMEX": "American Express",
    "DISC": "Discover",
    "DINERS": "Diners Club",
}


def build_decline_html(first_name, month_display, amount, reason_text, card_display):
    card_line = f' on your <strong>{card_display}</strong>' if card_display else ''
    
    return f"""<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
<p>Hi {first_name},</p>

<p>We attempted to process your <strong>{month_display}</strong> pool maintenance autopay of <strong>${amount:.2f}</strong>{card_line}, but unfortunately {reason_text}.</p>

<p>To avoid any interruption in service, please take one of the following steps:</p>

<ul style="margin: 15px 0;">
<li><strong>Update your payment method</strong> by calling our office at <strong>(912) 554-0636</strong></li>
<li><strong>Make a one-time payment</strong> via the invoice we'll send to this email</li>
</ul>

<p>If you've already resolved this, please disregard this notice.</p>

<p>Thank you for your continued business!</p>

<p style="margin-top: 20px;">
<strong>Jeff's Pool &amp; Spa Service</strong><br>
(912) 554-0636<br>
jpsbilling@jeffspoolspa.com
</p>
</div>"""


def get_gmail_credentials():
    sa_info = wmill.get_resource("u/carter/gmail_gcp_service_account")
    from_email = "jpsbilling@jeffspoolspa.com"
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=from_email
    )
    credentials.refresh(Request())
    return credentials, from_email


def send_email(credentials, from_email, to_email, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["To"] = to_email
    msg["From"] = f"Jeff's Pool & Spa Billing <{from_email}>"
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"},
        json={"raw": raw}
    )
    if resp.ok:
        return True, None
    else:
        return False, resp.text[:300]


def main(
    billing_month: str = "2026-02",
    send_to_statuses: list = ["charge_declined", "charge_api_failure", "error"],
    specific_customer_ids: list = None,
    test_email_override: str = None,
    dry_run: bool = True,
):
    """
    Send decline emails to customers whose autopay failed.
    
    Args:
        billing_month: YYYY-MM format
        send_to_statuses: Which transaction statuses to email
        specific_customer_ids: If provided, only send to these QBO customer IDs
        test_email_override: If set, send ALL emails to this address instead (for testing)
        dry_run: If True, don't actually send emails
    """
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"]
    )
    cur = conn.cursor()

    query = """
        SELECT t.id, t.qbo_customer_id, t.customer_name, t.email_address,
               t.charge_amount, t.status, t.error_message, t.decline_email_sent,
               t.card_type, t.last_four, t.payment_method, t.maint_amount
        FROM billing.autopay_transactions t
        WHERE t.billing_month = %s AND t.dry_run = false
          AND t.status = ANY(%s)
    """
    params = [billing_month, send_to_statuses]
    
    if specific_customer_ids:
        query += " AND t.qbo_customer_id = ANY(%s)"
        params.append(specific_customer_ids)
    
    query += " ORDER BY t.customer_name"
    cur.execute(query, params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    transactions = [dict(zip(cols, row)) for row in rows]

    credentials, from_email = get_gmail_credentials()
    month_display = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")
    
    results = {"sent": [], "skipped": [], "failed": [], "already_sent": []}

    for txn in transactions:
        txn_id = txn["id"]
        name = txn["customer_name"]
        email = test_email_override or txn["email_address"]
        amount = float(txn.get("maint_amount") or txn["charge_amount"] or 0)
        status = txn["status"]
        card_type = txn.get("card_type")
        last_four = txn.get("last_four")

        if txn["decline_email_sent"] and not test_email_override:
            results["already_sent"].append({"name": name, "email": txn["email_address"]})
            continue

        if not email:
            results["skipped"].append({"name": name, "reason": "no email"})
            continue

        if amount <= 0:
            results["skipped"].append({"name": name, "reason": "no charge amount"})
            continue

        card_display = ""
        if card_type and last_four:
            issuer = CARD_TYPE_DISPLAY.get(card_type.upper(), card_type)
            card_display = f"{issuer} ending in {last_four}"

        error_msg = txn.get("error_message") or ""
        if "exp" in error_msg.lower() or "invalid" in error_msg.lower():
            reason_text = "your card on file appears to have expired"
        elif status == "charge_declined":
            reason_text = "the charge was declined by your card issuer"
        else:
            reason_text = "we were unable to process the payment"

        first_name = name.split(",")[1].strip().title() if "," in name else name.split()[0].title()

        html_body = build_decline_html(first_name, month_display, amount, reason_text, card_display)
        subject = f"Payment Declined - {month_display} Pool Maintenance"

        if dry_run:
            results["sent"].append({
                "name": name, "email": email, "amount": amount,
                "card": card_display, "reason": reason_text, "dry_run": True
            })
            continue

        ok, err = send_email(credentials, from_email, email, subject, html_body)

        if ok:
            if not test_email_override:
                try:
                    cur.execute("""
                        UPDATE billing.autopay_transactions 
                        SET decline_email_sent = true, decline_email_sent_at = now(), updated_at = now()
                        WHERE id = %s::uuid
                    """, (str(txn_id),))
                    cur.execute("""
                        INSERT INTO billing.autopay_events 
                        (transaction_id, event_type, status_before, status_after, details)
                        VALUES (%s::uuid, 'decline_email_sent', %s, %s, %s::jsonb)
                    """, (str(txn_id), status, status, json.dumps({"to": txn["email_address"], "amount": amount, "card": card_display})))
                    conn.commit()
                except Exception as e:
                    try: conn.rollback()
                    except: pass
            
            results["sent"].append({"name": name, "email": email, "amount": amount, "card": card_display})
        else:
            results["failed"].append({"name": name, "email": email, "error": err})

    conn.close()
    
    return {
        "billing_month": billing_month,
        "dry_run": dry_run,
        "test_mode": bool(test_email_override),
        "total_transactions": len(transactions),
        "sent": len(results["sent"]),
        "skipped": len(results["skipped"]),
        "failed": len(results["failed"]),
        "already_sent": len(results["already_sent"]),
        "details": results
    }
