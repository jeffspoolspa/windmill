# Process a work order: apply credits, charge card/ACH or send invoice via QBO.
#
# Flow:
#   1. Acquire concurrency lock (billing_status → processing)
#   2. Read cached invoice from billing.invoices
#   3. Check if already processed (EmailSent)
#   4. Validate subtotal (WO vs QBO)
#   5. Apply matched credits from billing.open_credits WHERE matched_wo_number = wo_number
#   6. Charge remaining balance if payment_method = on_file
#   7. Update invoice in QBO (due date + memo)
#   8. Send invoice email
#   9. Log to billing.processing_attempts
#   10. Release lock (→ processed or needs_review)

import requests
import wmill
import psycopg2
import psycopg2.extras
import json
import uuid
from datetime import datetime

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"


# =============================================================================
# QBO API FUNCTIONS — preserved verbatim from service_billing_processing
# =============================================================================

def refresh_qbo_token():
    resource_path = QBO_RESOURCE
    resource = wmill.get_resource(resource_path)
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"])
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    return tokens["access_token"], resource["realm_id"]

def charge_card(card_id, amount, invoice_num, customer_name, access_token):
    request_id = str(uuid.uuid4())
    charge_resp = requests.post(
        "https://api.intuit.com/quickbooks/v4/payments/charges",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json", "Request-Id": request_id},
        json={"amount": f"{amount:.2f}", "currency": "USD", "capture": True,
              "cardOnFile": card_id, "context": {"mobile": False, "isEcommerce": True},
              "description": f"Invoice {invoice_num} - {customer_name}"}
    )
    if not charge_resp.ok:
        error_detail = charge_resp.text or f"HTTP {charge_resp.status_code}"
        try:
            ej = charge_resp.json()
            if "errors" in ej: error_detail = ej["errors"][0].get("message", error_detail)
        except Exception: pass
        return {"success": False, "error": error_detail, "payment_type": "card"}
    result = charge_resp.json()
    if result.get("status", "").upper() != "CAPTURED":
        return {"success": False, "error": f"Card {result.get('status')}", "payment_type": "card"}
    return {"success": True, "charge_id": result.get("id"), "amount": float(result.get("amount", 0)),
            "auth_code": result.get("authCode"), "status": result.get("status"),
            "card_last4": result.get("card", {}).get("number", "")[-4:],
            "card_type": result.get("card", {}).get("cardType"),
            "request_id": request_id, "payment_type": "card"}

def charge_bank_account(bank_id, amount, invoice_num, customer_name, access_token):
    request_id = str(uuid.uuid4())
    charge_resp = requests.post(
        "https://api.intuit.com/quickbooks/v4/payments/echecks",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json", "Request-Id": request_id},
        json={"amount": f"{amount:.2f}", "bankAccountOnFile": bank_id,
              "description": f"Invoice {invoice_num} - {customer_name}", "paymentMode": "WEB",
              "context": {"deviceInfo": {"macAddress":"","ipAddress":"","longitude":"","latitude":"","phoneNumber":""}}}
    )
    if not charge_resp.ok:
        error_detail = charge_resp.text or f"HTTP {charge_resp.status_code}"
        try:
            ej = charge_resp.json()
            if "errors" in ej: error_detail = ej["errors"][0].get("message", error_detail)
        except Exception: pass
        return {"success": False, "error": error_detail, "payment_type": "ach"}
    result = charge_resp.json()
    if result.get("status", "").upper() not in ["PENDING", "SUCCEEDED"]:
        return {"success": False, "error": f"ACH {result.get('status')}", "payment_type": "ach"}
    return {"success": True, "charge_id": result.get("id"), "amount": float(result.get("amount", 0)),
            "auth_code": result.get("authCode", ""), "status": result.get("status"),
            "card_last4": result.get("bankAccount", {}).get("accountNumber", "")[-4:],
            "card_type": "ACH", "request_id": request_id, "payment_type": "ach"}

def record_payment(customer_id, invoice_id, amount, charge_result, wo_num, invoice_num, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"}
    cid = charge_result.get("charge_id",""); auth = charge_result.get("auth_code","")
    ct = charge_result.get("card_type",""); cl4 = charge_result.get("card_last4","")
    note = f"Auto-charge | WO# {wo_num} | Inv# {invoice_num} | Charge ID: {cid} | Auth: {auth} | {ct} x{cl4} | {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    resp = requests.post(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment", headers=headers,
        json={"CustomerRef": {"value": customer_id}, "TotalAmt": amount,
              "PaymentMethodRef": {"value": "20" if charge_result.get("payment_type")=="ach" else "21"},
              "PaymentRefNum": wo_num, "TxnDate": datetime.now().strftime("%Y-%m-%d"),
              "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
              "PrivateNote": note,
              "CreditCardPayment": {"CreditChargeInfo": {"ProcessPayment": True, "Amount": amount},
                                    "CreditChargeResponse": {"Status": "Completed", "CCTransId": cid}},
              "TxnSource": "IntuitPayment"})
    if not resp.ok: return {"success": False, "error": resp.text[:300]}
    pmt = resp.json().get("Payment", {})
    return {"success": True, "payment_id": pmt.get("Id"), "payment_ref": pmt.get("PaymentRefNum")}

def update_invoice(invoice_id, memo, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"}
    inv_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}", headers=headers)
    if not inv_resp.ok: return {"success": False, "error": f"Fetch failed: {inv_resp.status_code}"}
    inv = inv_resp.json().get("Invoice", {})
    ud = {"Id": invoice_id, "SyncToken": inv.get("SyncToken"), "sparse": True, "DueDate": datetime.now().strftime("%Y-%m-%d")}
    if memo: ud["PrivateNote"] = memo; ud["CustomerMemo"] = {"value": memo}
    resp = requests.post(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice", headers=headers, json=ud)
    return {"success": True} if resp.ok else {"success": False, "error": resp.text[:300]}

def send_invoice_email(invoice_id, customer_id, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    inv_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}", headers=headers)
    if inv_resp.ok and inv_resp.json().get("Invoice", {}).get("EmailStatus") == "EmailSent":
        return {"success": True, "skipped": True}
    cust_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{customer_id}", headers=headers)
    email = cust_resp.json().get("Customer", {}).get("PrimaryEmailAddr", {}).get("Address") if cust_resp.ok else None
    url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}/send"
    if email: url += f"?sendTo={email}"
    resp = requests.post(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/octet-stream"})
    return {"success": True, "sent_to": email} if resp.ok else {"success": False, "error": resp.text[:300]}

def send_payment_receipt(payment_id, customer_id, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    cust_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{customer_id}", headers=headers)
    email = cust_resp.json().get("Customer", {}).get("PrimaryEmailAddr", {}).get("Address") if cust_resp.ok else None
    if not email: return {"success": False, "error": "No email"}
    resp = requests.post(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{payment_id}/send?sendTo={email}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/octet-stream"})
    return {"success": True, "sent_to": email} if resp.ok else {"success": False, "error": resp.text[:300]}

def lookup_invoice(invoice_num, access_token, realm_id):
    resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": f"SELECT * FROM Invoice WHERE DocNumber = '{invoice_num}'"})
    if not resp.ok: return {"found": False, "error": f"Query failed: {resp.status_code}"}
    invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])
    if not invoices: return {"found": False, "error": "Not found"}
    inv = invoices[0]; total = float(inv.get("TotalAmt", 0)); tax = float(inv.get("TxnTaxDetail", {}).get("TotalTax", 0))
    return {"found": True, "invoice_id": inv.get("Id"), "customer_id": inv.get("CustomerRef", {}).get("value"),
            "subtotal": round(total - tax, 2), "balance": float(inv.get("Balance", 0)), "email_status": inv.get("EmailStatus")}


# =============================================================================
# SUPABASE + CREDIT HELPERS
# =============================================================================

def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(host=sb["host"], port=sb.get("port", 6543), dbname=sb.get("dbname", "postgres"),
                            user=sb["user"], password=sb["password"], sslmode=sb.get("sslmode", "require"))

def acquire_lock(conn, wo_number):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE public.work_orders SET billing_status = 'processing', billing_status_set_at = now() WHERE wo_number = %s AND billing_status = 'ready_to_process' RETURNING *", (wo_number,))
    row = cur.fetchone(); conn.commit(); cur.close()
    return dict(row) if row else None

def release_lock(conn, wo_number, status, needs_review_reason=None):
    cur = conn.cursor()
    cur.execute("UPDATE public.work_orders SET billing_status = %s, billing_status_set_at = now(), needs_review_reason = %s, last_synced_at = now() WHERE wo_number = %s",
                (status, needs_review_reason, wo_number)); conn.commit(); cur.close()

def get_cached_invoice(conn, invoice_number):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM billing.invoices WHERE doc_number = %s", (invoice_number,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None

def get_cached_payment_method(conn, qbo_customer_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT * FROM billing.customer_payment_methods WHERE qbo_customer_id = %s AND is_active = true
                   ORDER BY CASE type WHEN 'card' THEN 0 ELSE 1 END, is_default DESC, fetched_at DESC LIMIT 1""", (qbo_customer_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None

def get_matched_credits(conn, wo_number):
    """Get credits matched to this WO from billing.open_credits."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT qbo_payment_id, type, unapplied_amt, matched_amount, match_reason
                   FROM billing.open_credits WHERE matched_wo_number = %s AND matched_amount > 0
                   ORDER BY matched_amount DESC""", (wo_number,))
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows

def apply_credit_in_qbo(credit_id, credit_type, invoice_id, amount, access_token, realm_id):
    """Link a QBO Payment or CreditMemo to an Invoice."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"}
    try:
        if credit_type == "credit_memo":
            cm_id = credit_id.replace("CM-", "")
            cm_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/creditmemo/{cm_id}", headers=headers)
            if not cm_resp.ok: return {"success": False, "error": f"Fetch CreditMemo failed: {cm_resp.status_code}"}
            resp = requests.post(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment", headers=headers,
                json={"CustomerRef": cm_resp.json().get("CreditMemo", {}).get("CustomerRef"), "TotalAmt": 0,
                      "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": cm_id, "TxnType": "CreditMemo"}, {"TxnId": invoice_id, "TxnType": "Invoice"}]}]})
            return {"success": True} if resp.ok else {"success": False, "error": f"CM apply failed: {resp.text[:300]}"}
        else:
            pmt_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{credit_id}", headers=headers)
            if not pmt_resp.ok: return {"success": False, "error": f"Fetch Payment failed: {pmt_resp.status_code}"}
            payment = pmt_resp.json().get("Payment", {})
            payment.setdefault("Line", []).append({"Amount": amount, "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]})
            payment["sparse"] = True
            resp = requests.post(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment", headers=headers, json=payment)
            return {"success": True} if resp.ok else {"success": False, "error": f"Payment apply failed: {resp.text[:300]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def log_processing_attempt(conn, wo_number, result):
    cur = conn.cursor()
    cr = result.get("charge_result") or {}
    cur.execute("""INSERT INTO billing.processing_attempts (wo_number, invoice_number, qbo_invoice_id, attempted_at,
                   status, payment_method, charge_amount, charge_result, credits_applied, email_sent, error_message, raw_result)
                   VALUES (%s,%s,%s,now(),%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb)""",
        (wo_number, result.get("invoice_number"), result.get("qbo_invoice_id"), result.get("status","failed"),
         result.get("payment_method"), result.get("charge_amount"),
         json.dumps(cr) if cr else None, json.dumps(result.get("credits_applied")) if result.get("credits_applied") else None,
         result.get("email_sent", False), result.get("error_message"), json.dumps(result) if result else None))
    conn.commit(); cur.close()

def update_invoice_cache(conn, invoice_number, balance, email_status):
    cur = conn.cursor()
    cur.execute("UPDATE billing.invoices SET balance = %s, email_status = %s, fetched_at = now() WHERE doc_number = %s",
                (balance, email_status, invoice_number)); conn.commit(); cur.close()


# =============================================================================
# MAIN
# =============================================================================

def process_one(conn, wo_number, access_token, realm_id, dry_run=False):
    result = {"wo_number": wo_number, "status": None, "error_message": None, "invoice_number": None,
              "qbo_invoice_id": None, "payment_method": None, "charge_amount": None,
              "charge_result": None, "credits_applied": None, "email_sent": False}

    wo = acquire_lock(conn, wo_number)
    if not wo:
        result["status"] = "skipped"; result["error_message"] = "Not in ready_to_process"; return result

    invoice_number = wo.get("invoice_number"); payment_method = wo.get("payment_method")
    customer_name = wo.get("customer", ""); wo_subtotal = float(wo.get("sub_total") or 0)
    description = wo.get("work_description") or ""
    result["invoice_number"] = invoice_number; result["payment_method"] = payment_method

    try:
        # Read cached invoice
        cached = get_cached_invoice(conn, invoice_number)
        if not cached:
            live = lookup_invoice(invoice_number, access_token, realm_id)
            if not live["found"]:
                result["status"] = "failed"; result["error_message"] = f"Invoice {invoice_number} not found"
                release_lock(conn, wo_number, "needs_review", result["error_message"])
                log_processing_attempt(conn, wo_number, result); return result
            qbo_invoice_id=live["invoice_id"]; qbo_customer_id=live["customer_id"]
            qbo_subtotal=live["subtotal"]; qbo_balance=live["balance"]; email_status=live["email_status"]
        else:
            qbo_invoice_id=cached["qbo_invoice_id"]; qbo_customer_id=cached.get("qbo_customer_id")
            qbo_subtotal=float(cached.get("subtotal") or 0); qbo_balance=float(cached.get("balance") or 0)
            email_status=cached.get("email_status")
        result["qbo_invoice_id"] = qbo_invoice_id

        # Already sent?
        if email_status == "EmailSent":
            result["status"]="success"; result["email_sent"]=True
            release_lock(conn, wo_number, "processed")
            update_invoice_cache(conn, invoice_number, qbo_balance, email_status)
            log_processing_attempt(conn, wo_number, result); return result

        # Subtotal check
        if wo_subtotal > 0 and qbo_subtotal > 0 and abs(qbo_subtotal - wo_subtotal) >= 0.02:
            result["status"]="failed"; result["error_message"]=f"Subtotal mismatch: WO ${wo_subtotal:.2f} vs QBO ${qbo_subtotal:.2f}"
            release_lock(conn, wo_number, "needs_review", result["error_message"])
            log_processing_attempt(conn, wo_number, result); return result

        if dry_run:
            matched = get_matched_credits(conn, wo_number)
            pm = get_cached_payment_method(conn, qbo_customer_id) if payment_method == "on_file" and qbo_balance > 0 else None
            pm_info = f"{pm['type']} x{pm['last_four']}" if pm else "n/a"
            credit_total = sum(float(c['matched_amount']) for c in matched)
            result["status"]="skipped"
            result["credits_applied"] = [{"id": c["qbo_payment_id"], "amount": float(c["matched_amount"])} for c in matched]
            result["error_message"] = f"dry_run — {len(matched)} credits (${credit_total:.2f}), remainder ${max(0, qbo_balance - credit_total):.2f} via {pm_info}"
            release_lock(conn, wo_number, "ready_to_process"); return result

        # ── APPLY MATCHED CREDITS ────────────────────────────────
        matched_credits = get_matched_credits(conn, wo_number)
        credits_applied = []; remaining_balance = qbo_balance

        for mc in matched_credits:
            apply_amount = min(float(mc["matched_amount"]), remaining_balance)
            if apply_amount <= 0: break
            ar = apply_credit_in_qbo(mc["qbo_payment_id"], mc["type"], qbo_invoice_id, apply_amount, access_token, realm_id)
            credits_applied.append({"credit_id": mc["qbo_payment_id"], "amount": apply_amount, "success": ar["success"], "error": ar.get("error")})
            if ar["success"]:
                remaining_balance -= apply_amount
                cur = conn.cursor()
                cur.execute("UPDATE billing.open_credits SET unapplied_amt = GREATEST(unapplied_amt - %s, 0) WHERE qbo_payment_id = %s", (apply_amount, mc["qbo_payment_id"]))
                conn.commit(); cur.close()
            else:
                print(f"  credit apply failed: {mc['qbo_payment_id']}: {ar.get('error')}")
        result["credits_applied"] = credits_applied
        if credits_applied:
            print(f"  applied {len(credits_applied)} credits, remaining: ${remaining_balance:.2f}")

        # ── CHARGE REMAINDER ─────────────────────────────────────
        if payment_method == "on_file" and remaining_balance > 0:
            pm = get_cached_payment_method(conn, qbo_customer_id)
            if not pm:
                result["status"]="failed"; result["error_message"]="No payment method cached"
                release_lock(conn, wo_number, "needs_review", result["error_message"])
                log_processing_attempt(conn, wo_number, result); return result
            charge_amount = remaining_balance; result["charge_amount"] = charge_amount
            charge_result = charge_card(pm["qbo_payment_method_id"], charge_amount, invoice_number, customer_name, access_token) if pm["type"] == "card" else charge_bank_account(pm["qbo_payment_method_id"], charge_amount, invoice_number, customer_name, access_token)
            result["charge_result"] = charge_result
            if not charge_result["success"]:
                label = "Card Declined" if pm["type"]=="card" else "ACH Failed"
                result["status"]="failed"; result["error_message"]=f"{label}: {charge_result.get('error')}"
                release_lock(conn, wo_number, "needs_review", result["error_message"])
                log_processing_attempt(conn, wo_number, result); return result
            if not charge_result.get("charge_id"):
                result["status"]="failed"; result["error_message"]="Charge response unclear"
                release_lock(conn, wo_number, "needs_review", result["error_message"])
                log_processing_attempt(conn, wo_number, result); return result
            pr = record_payment(qbo_customer_id, qbo_invoice_id, charge_amount, charge_result, wo_number, invoice_number, access_token, realm_id)
            if not pr["success"]:
                label = "CHARGED" if pm["type"]=="card" else "ACH INITIATED"
                result["status"]="partial"; result["error_message"]=f"{label} ${charge_amount:.2f} but QBO payment failed: {pr.get('error')}"
                release_lock(conn, wo_number, "needs_review", result["error_message"])
                log_processing_attempt(conn, wo_number, result); return result
            send_payment_receipt(pr["payment_id"], qbo_customer_id, access_token, realm_id)

        # ── UPDATE INVOICE + SEND EMAIL ──────────────────────────
        ur = update_invoice(qbo_invoice_id, description, access_token, realm_id)
        if not ur["success"]:
            result["status"]="failed"; result["error_message"]=f"Invoice update failed: {ur.get('error')}"
            release_lock(conn, wo_number, "needs_review", result["error_message"])
            log_processing_attempt(conn, wo_number, result); return result

        er = send_invoice_email(qbo_invoice_id, qbo_customer_id, access_token, realm_id)
        result["email_sent"] = er["success"]
        if not er["success"] and not er.get("skipped"):
            result["status"]="failed"; result["error_message"]=f"Email failed: {er.get('error')}"
            release_lock(conn, wo_number, "needs_review", result["error_message"])
            log_processing_attempt(conn, wo_number, result); return result

        # ── SUCCESS ──────────────────────────────────────────────
        live_check = lookup_invoice(invoice_number, access_token, realm_id)
        final_balance = live_check.get("balance", qbo_balance) if live_check.get("found") else qbo_balance
        result["status"] = "success"
        release_lock(conn, wo_number, "processed")
        update_invoice_cache(conn, invoice_number, final_balance, "EmailSent")
        log_processing_attempt(conn, wo_number, result)
        return result

    except Exception as e:
        result["status"]="failed"; result["error_message"]=str(e)[:500]
        try: release_lock(conn, wo_number, "needs_review", result["error_message"]); log_processing_attempt(conn, wo_number, result)
        except Exception: pass
        return result


def main(wo_numbers: list[str] | None = None, wo_number: str | None = None, dry_run: bool = False):
    targets = wo_numbers or ([wo_number] if wo_number else [])
    if not targets: return {"error": "Provide wo_number or wo_numbers"}
    print(f"=== process_work_order ({len(targets)} WOs, dry_run={dry_run}) ===")
    conn = get_db_conn(); access_token, realm_id = refresh_qbo_token()
    results = []; stats = {"success": 0, "failed": 0, "skipped": 0, "partial": 0}
    for i, wn in enumerate(targets):
        print(f"  [{i+1}/{len(targets)}] WO {wn}...")
        r = process_one(conn, wn, access_token, realm_id, dry_run)
        results.append(r); s = r.get("status","failed"); stats[s] = stats.get(s,0) + 1
        print(f"    -> {s}" + (f": {r.get('error_message')}" if r.get("error_message") else ""))
    conn.close()
    print(f"=== done: {stats} ===")
    return {"dry_run": dry_run, "total": len(targets), "stats": stats, "results": results}
