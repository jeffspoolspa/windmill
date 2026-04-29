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
    "VISA": "Visa", "MC": "Mastercard", "AMEX": "American Express",
    "DISC": "Discover", "DINERS": "Diners Club",
}

def main(customer_result: dict, access_token: str, realm_id: str, billing_month: str, dry_run: bool = True):
    status = customer_result.get("status")
    txn_id = customer_result.get("transaction_id")
    qbo_id = customer_result.get("qbo_customer_id")
    name = customer_result.get("customer_name")
    headers_qbo = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["dbname"], user=db["user"], password=db["password"])
    verification = {
        "customer_name": name,
        "qbo_customer_id": qbo_id,
        "transaction_id": txn_id,
        "original_status": status,
        "verified": False,
        "email_sent": False,
        "decline_email_sent": False,
        "decline_email_error": None,
        "notes": [],
        "errors": [],
    }

    def safe_close():
        try:
            conn.close()
        except Exception as e:
            verification["errors"].append(f"Connection close error: {str(e)[:100]}")

    def update_txn(new_status, **kwargs):
        if not txn_id:
            verification["errors"].append(f"update_txn called with no txn_id for {name} (status={new_status})")
            return
        try:
            cur = conn.cursor()
            sets = ["status = %s", "updated_at = now()"]; vals = [new_status]
            now_fields = kwargs.pop("_now_fields", [])
            for k, v in kwargs.items(): sets.append(f"{k} = %s"); vals.append(v)
            for nf in now_fields: sets.append(f"{nf} = now()")
            vals.append(txn_id)
            cur.execute(f"UPDATE billing.autopay_transactions SET {', '.join(sets)} WHERE id = %s::uuid", vals)
            conn.commit()
        except Exception as e:
            verification["errors"].append(f"DB update error for {name}: {str(e)[:150]}")
            try: conn.rollback()
            except: pass

    def log_event(event_type, status_before, status_after, details=None):
        if not txn_id:
            verification["errors"].append(f"log_event called with no txn_id for {name} (event={event_type})")
            return
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO billing.autopay_events (transaction_id, event_type, status_before, status_after, details) VALUES (%s::uuid, %s, %s, %s, %s::jsonb)", (txn_id, event_type, status_before, status_after, json.dumps(details or {})))
            conn.commit()
        except Exception as e:
            verification["errors"].append(f"Event log error for {name}: {str(e)[:150]}")
            try: conn.rollback()
            except: pass

    def get_card_info_from_txn():
        if not txn_id:
            verification["errors"].append(f"get_card_info called with no txn_id for {name}")
            return "", ""
        try:
            cur = conn.cursor()
            cur.execute("SELECT card_type, last_four FROM billing.autopay_transactions WHERE id = %s::uuid", (txn_id,))
            row = cur.fetchone()
            if row:
                return row[0] or "", row[1] or ""
            else:
                verification["errors"].append(f"No transaction record found for txn_id={txn_id} ({name})")
        except Exception as e:
            verification["errors"].append(f"Card info lookup error for {name}: {str(e)[:100]}")
        return "", ""

    def get_consecutive_declines():
        try:
            cur = conn.cursor()
            cur.execute("SELECT consecutive_declines FROM billing.autopay_customers WHERE qbo_customer_id = %s", (qbo_id,))
            row = cur.fetchone()
            if row:
                return row[0] or 0
        except Exception as e:
            verification["errors"].append(f"Consecutive declines lookup error for {name}: {str(e)[:100]}")
        return 0

    def send_decline_email(to_email, customer_name, amount, month_display, card_type="", last_four="", error_message="", is_escalated=False, total_outstanding=0):
        sa_info = wmill.get_resource("u/carter/gmail_gcp_service_account")
        if not sa_info:
            return False, "Gmail service account resource is empty or missing"

        from_email = "jpsbilling@jeffspoolspa.com"
        credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            subject=from_email
        )
        credentials.refresh(Request())

        first_name = customer_name.split(",")[1].strip().title() if "," in customer_name else customer_name.split()[0].title()

        card_display = ""
        if card_type and last_four:
            issuer = CARD_TYPE_DISPLAY.get(card_type.upper(), card_type)
            card_display = f"{issuer} ending in {last_four}"

        card_line = f' on your <strong>{card_display}</strong>' if card_display else ''

        if is_escalated:
            subject = f"ACTION REQUIRED - Outstanding Balance ${total_outstanding:.2f} - Pool Service"
            html_body = f"""<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
<p>Hi {first_name},</p>

<p>We attempted to process your pool maintenance autopay of <strong>${amount:.2f}</strong>{card_line}, but unfortunately the payment was <strong>declined</strong> for the second consecutive month.</p>

<p>Your total outstanding maintenance balance is <strong>${total_outstanding:.2f}</strong>.</p>

<p style="color: #c0392b; font-weight: bold;">Service will be paused until the full balance is paid and a new payment method is provided on file.</p>

<p>To resolve this and avoid any interruption in service, please contact our office as soon as possible:</p>

<ul style="margin: 15px 0;">
<li><strong>Call us</strong> at <strong>(912) 554-0636</strong> to update your payment method and pay the outstanding balance</li>
<li><strong>Make a payment</strong> via the invoice we'll send to this email</li>
</ul>

<p>We value your business and want to ensure your pool continues to receive the care it needs. Please reach out at your earliest convenience so we can get this resolved.</p>

<p style="margin-top: 20px;">
<strong>Jeff's Pool &amp; Spa Service</strong><br>
(912) 554-0636<br>
jpsbilling@jeffspoolspa.com
</p>
</div>"""
        else:
            subject = f"Payment Declined - {month_display} Pool Maintenance"
            html_body = f"""<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
<p>Hi {first_name},</p>

<p>We attempted to process your <strong>{month_display}</strong> pool maintenance autopay of <strong>${amount:.2f}</strong>{card_line}, but unfortunately the payment was <strong>declined</strong>.</p>

<p>This can happen if your card has expired, been replaced, or if there are insufficient funds. To avoid any interruption in service, please take one of the following steps:</p>

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

        msg = MIMEMultipart("alternative")
        msg["To"] = to_email
        msg["From"] = f"Jeff's Pool & Spa Billing <{from_email}>"
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        send_resp = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"},
            json={"raw": raw}
        )
        if send_resp.ok:
            return True, None
        else:
            return False, f"Gmail API error {send_resp.status_code}: {send_resp.text[:200]}"

    try:
        if status in ("no_invoice", "no_payment_method", "dry_run_success"):
            verification["notes"].append(f"Skipped: status={status}")
            return verification

        if status in ("charge_declined", "charge_api_failure", "error"):
            if status == "error":
                error_step = customer_result.get("error_step", "")
                if error_step not in ("charge_api_failure", "charge_attempted"):
                    verification["notes"].append(f"Skipped non-charge error: step={error_step}")
                    return verification

            customer_email = customer_result.get("customer_email") or customer_result.get("email")
            amount = customer_result.get("charge_amount") or customer_result.get("amount_charged") or 0
            error_message = customer_result.get("error_message") or customer_result.get("charge_error") or ""
            month_display = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")

            if amount == 0:
                verification["errors"].append(f"WARNING: decline email amount is $0.00 for {name} - charge_amount and amount_charged both missing/zero")

            card_type, last_four = get_card_info_from_txn()
            if not card_type and not last_four:
                verification["notes"].append(f"No card info available for {name} - email will omit card details")

            consecutive_declines = get_consecutive_declines()
            is_escalated = consecutive_declines >= 2
            if is_escalated:
                verification["notes"].append(f"ESCALATED: {name} has {consecutive_declines} consecutive decline(s) - sending escalated email with service pause warning")

            total_outstanding = amount

            if customer_email and not dry_run:
                try:
                    sent, err_detail = send_decline_email(
                        customer_email, name, amount, month_display,
                        card_type, last_four, error_message,
                        is_escalated=is_escalated, total_outstanding=total_outstanding
                    )
                except Exception as e:
                    sent = False
                    err_detail = f"Exception: {type(e).__name__}: {str(e)[:200]}"

                verification["decline_email_sent"] = sent
                if sent:
                    card_display = ""
                    if card_type and last_four:
                        issuer = CARD_TYPE_DISPLAY.get(card_type.upper(), card_type)
                        card_display = f"{issuer} ending in {last_four}"
                    email_type = "escalated decline" if is_escalated else "decline"
                    verification["notes"].append(f"{email_type.title()} email sent to {customer_email}")
                    update_txn(status, decline_email_sent=True, _now_fields=["decline_email_sent_at"])
                    log_event("decline_email_sent", status, status, {
                        "to": customer_email, "amount": amount, "card": card_display,
                        "escalated": is_escalated, "consecutive_declines": consecutive_declines,
                        "total_outstanding": total_outstanding
                    })
                else:
                    verification["decline_email_error"] = err_detail
                    verification["errors"].append(f"DECLINE EMAIL FAILED for {name} ({customer_email}): {err_detail}")
                    verification["notes"].append(f"FAILED to send decline email to {customer_email}: {err_detail}")
                    log_event("decline_email_failed", status, status, {"to": customer_email, "error": err_detail, "escalated": is_escalated})
            elif customer_email and dry_run:
                email_type = "escalated decline" if is_escalated else "decline"
                verification["notes"].append(f"DRY RUN: Would send {email_type} email to {customer_email} for ${amount:.2f} (consecutive_declines={consecutive_declines})")
            else:
                verification["notes"].append("No email on file for decline notification")
                verification["errors"].append(f"NO EMAIL ON FILE for decline notification: {name}")

            return verification

        if status != "awaiting_verification":
            verification["errors"].append(f"UNEXPECTED STATUS for {name}: {status} - no action taken")
            verification["notes"].append(f"Unexpected status: {status}")
            return verification

        charge_id = customer_result.get("charge_id")
        payment_id = customer_result.get("payment_id")
        amount_charged = customer_result.get("amount_charged")

        if not payment_id:
            verification["errors"].append(f"NO PAYMENT ID for {name} in awaiting_verification status")

        payment_verified = False
        if payment_id:
            try:
                pay_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{payment_id}", headers=headers_qbo)
                if pay_resp.ok:
                    pay_data = pay_resp.json().get("Payment", {})
                    pay_amount = float(pay_data.get("TotalAmt", 0))
                    if abs(pay_amount - amount_charged) < 0.01:
                        payment_verified = True
                        verification["notes"].append(f"Payment #{payment_id} verified: ${pay_amount:.2f}")
                    else:
                        verification["errors"].append(f"AMOUNT MISMATCH for {name}: Expected ${amount_charged:.2f}, found ${pay_amount:.2f}")
                        verification["notes"].append(f"WARNING: Amount mismatch. Expected ${amount_charged:.2f}, found ${pay_amount:.2f}")
                    cc_txn_id = pay_data.get("CreditCardPayment", {}).get("CreditChargeResponse", {}).get("CCTransId", "")
                    if charge_id and cc_txn_id == charge_id:
                        verification["notes"].append("Charge ID matches")
                    elif charge_id:
                        verification["errors"].append(f"CHARGE ID MISMATCH for {name}: expected={charge_id}, found={cc_txn_id}")
                        verification["notes"].append("WARNING: Charge ID mismatch")
                    lines = pay_data.get("Line", [])
                    applied = sum(1 for l in lines for t in l.get("LinkedTxn", []) if t.get("TxnType") == "Invoice")
                    expected = len(customer_result.get("invoices_paid", []))
                    if applied == expected:
                        verification["notes"].append(f"All {applied} invoice(s) applied")
                    else:
                        verification["errors"].append(f"INVOICE COUNT MISMATCH for {name}: Expected {expected}, found {applied}")
                        verification["notes"].append(f"WARNING: Expected {expected} invoices, found {applied}")
                else:
                    verification["errors"].append(f"PAYMENT LOOKUP FAILED for {name}: HTTP {pay_resp.status_code} - {pay_resp.text[:150]}")
                    verification["notes"].append(f"Payment lookup failed: HTTP {pay_resp.status_code}")
            except Exception as e:
                verification["errors"].append(f"VERIFICATION ERROR for {name}: {type(e).__name__}: {str(e)[:150]}")
                verification["notes"].append(f"Verification error: {str(e)[:100]}")

        verification["verified"] = payment_verified

        if payment_verified:
            update_txn("awaiting_verification", verified=True, _now_fields=["verified_at"])
            log_event("verification_passed", "awaiting_verification", "awaiting_verification", {"payment_id": payment_id})

            customer_email = customer_result.get("customer_email")
            if not customer_email:
                verification["notes"].append("Email skipped - no email on file")
                verification["errors"].append(f"NO EMAIL for receipt: {name}")
                update_txn("completed", verified=True)
            else:
                receipt_ok = False
                invoices_sent = 0
                email_error = None
                try:
                    if payment_id:
                        send_resp = requests.post(
                            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{payment_id}/send?sendTo={customer_email}",
                            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/octet-stream"}
                        )
                        receipt_ok = send_resp.ok
                        if not send_resp.ok:
                            verification["errors"].append(f"RECEIPT EMAIL FAILED for {name} ({customer_email}): HTTP {send_resp.status_code} - {send_resp.text[:150]}")

                    inv_resp = requests.get(
                        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
                        headers=headers_qbo,
                        params={"query": f"SELECT * FROM Invoice WHERE CustomerRef = '{qbo_id}'"}
                    )
                    if inv_resp.ok:
                        paid_nums = set(customer_result.get("invoices_paid", []))
                        invoices_already_emailed = 0
                        for inv in inv_resp.json().get("QueryResponse", {}).get("Invoice", []):
                            if inv.get("DocNumber") in paid_nums:
                                if inv.get("EmailStatus") == "EmailSent":
                                    invoices_already_emailed += 1
                                    verification["notes"].append(f"Invoice #{inv.get('DocNumber')} already emailed - skipped")
                                    continue
                                try:
                                    er = requests.post(
                                        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{inv['Id']}/send?sendTo={customer_email}",
                                        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/octet-stream"}
                                    )
                                    if er.ok:
                                        invoices_sent += 1
                                    else:
                                        verification["errors"].append(f"INVOICE EMAIL FAILED for {name} inv#{inv.get('DocNumber')}: HTTP {er.status_code} - {er.text[:100]}")
                                except Exception as ie:
                                    verification["errors"].append(f"INVOICE EMAIL ERROR for {name} inv#{inv.get('DocNumber')}: {type(ie).__name__}: {str(ie)[:100]}")
                        if invoices_already_emailed > 0:
                            verification["notes"].append(f"{invoices_already_emailed} invoice(s) already emailed, {invoices_sent} newly sent")
                    else:
                        verification["errors"].append(f"INVOICE QUERY FAILED for {name}: HTTP {inv_resp.status_code} - {inv_resp.text[:150]}")

                except Exception as e:
                    email_error = f"{type(e).__name__}: {str(e)[:150]}"
                    verification["errors"].append(f"EMAIL SEND ERROR for {name}: {email_error}")

                verification["email_sent"] = receipt_ok or invoices_sent > 0
                if email_error:
                    verification["notes"].append(f"Email error for {customer_email}: {email_error}")
                    update_txn("completed", verified=True, receipt_emailed=False, invoice_emailed=False, email_address=customer_email, _now_fields=["emailed_at"])
                    log_event("email_error", "awaiting_verification", "completed", {"to": customer_email, "error": email_error})
                else:
                    verification["notes"].append(f"Emailed {customer_email} (receipt: {'OK' if receipt_ok else 'FAIL'}, invoices: {invoices_sent})")
                    update_txn("completed", verified=True, receipt_emailed=receipt_ok, invoice_emailed=(invoices_sent > 0), email_address=customer_email, _now_fields=["emailed_at"])
                    log_event("email_sent", "awaiting_verification", "completed", {"to": customer_email, "receipt": receipt_ok, "invoices": invoices_sent})
        else:
            verification["notes"].append("Not fully verified - flagged for review")
            verification["errors"].append(f"VERIFICATION FAILED for {name} - flagged for review")
            update_txn("needs_review", verified=False)
            log_event("verification_failed", "awaiting_verification", "needs_review", {"reason": "verification checks failed"})

        return verification

    finally:
        safe_close()
