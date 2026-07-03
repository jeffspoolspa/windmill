# requirements:
# psycopg2-binary
# requests
# wmill

# f/billing/process_maint_period
#
# Charge / send maintenance billing periods in processing_status =
# 'ready_to_process'. The maintenance counterpart of
# f/service_billing/process_invoice — SAME billing.processing_attempts table,
# SAME write-ahead-log + idempotency-key method (key generated once, persisted
# BEFORE the charge, reused on retry; Intuit dedupes on Request-Id).
#
# Module: docs/flows/monthly-maintenance-billing/index.md
# Status: [active]
# Concurrency key: qbo_writer (concurrent_limit 1 — money movement serializes)
#
# Triggered by:
#   - /api/maintenance-billing/process (the Ready to Process tab's
#     "Process selected" button; dry_run first)
#   - manual (single period retry / orphan recovery)
#
# Tables touched:
#   billing_audit.task_billing_periods  [read]  the ready periods (HARD gate:
#                                               only ready_to_process charges)
#   billing.processing_attempts         [r/w]   the WAL: pending ->
#                                               charge_uncertain|charge_declined|
#                                               charge_succeeded -> payment_orphan|
#                                               email_failed|succeeded
#   billing.autopay_customers           [r/w]   the charge route (period ->
#                                               autopay_customer_id ->
#                                               payment_method_id); declines bump
#                                               consecutive_declines/payment_status
#   billing.customer_payment_methods    [read]  the exact card/bank charged
#   billing.autopay_transactions        [write] the maintenance reporting record
#                                               (Processing tab + projection's
#                                               autopay_charged gate)
#   billing.invoices                    [r/w]   balance/email_status cache update
#                                               after charge+send (fires the
#                                               auto-promote trigger)
#
# External APIs:
#   - Intuit Payments v4: charges (card) / echecks (ACH), Request-Id = the
#     persisted idempotency key
#   - QBO v3: Payment create (CCTransId reconciliation), payment/{id}/send
#     (RECEIPT — sent FIRST), invoice/{id}/send (the invoice copy, sent after)
#
# Why this exists:
#   Processing used to be the roster-driven monthly_autopay flow (charge
#   everyone unpaid minus HIGH flags). The pipeline makes charging
#   status-driven: ONLY ready_to_process periods are chargeable, through the
#   roster row's linked payment method, receipt before invoice — so a flagged
#   customer structurally cannot be charged before review (Carter, 2026-07-03).

import json
import uuid
import psycopg2
import psycopg2.extras
import requests
import wmill
from datetime import datetime, date
from decimal import Decimal

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
QBO_PMT_METHOD_CC = "21"
QBO_PMT_METHOD_ACH = "20"


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _dumps(obj):
    return json.dumps(obj, default=_json_default)


def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]), timeout=30,
    )
    if not resp.ok:
        raise Exception(f"QBO token refresh failed: {resp.status_code} - {resp.text}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"], resource["realm_id"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


# ── charge helpers (cloned from f/service_billing/process_invoice) ──────────

def _classify_charge_response(resp, payment_type):
    if resp is None:
        return "uncertain"
    sc = resp.status_code
    if sc >= 500:
        return "uncertain"
    if not resp.ok:
        return "declined"
    try:
        result = resp.json()
        status = (result.get("status") or "").upper()
        if payment_type == "card":
            return "success" if status == "CAPTURED" else "declined"
        return "success" if status in ("PENDING", "SUCCEEDED") else "declined"
    except Exception:
        return "uncertain"


def extract_charge_error(resp, body=None):
    if resp is None:
        return "no response from Intuit (network error)"
    if body is None:
        try:
            body = resp.json()
        except Exception:
            body = None
    sc = resp.status_code
    if body is None:
        text = (resp.text or "").strip()
        if text.startswith("<") or "<html" in text[:200].lower():
            return f"HTTP {sc}: gateway returned HTML (likely 5xx upstream)"
        return f"HTTP {sc}: {text[:300] if text else 'empty body'}"
    errors = body.get("errors") or []
    if errors:
        e = errors[0] if isinstance(errors[0], dict) else {}
        parts = [p for p in [e.get("message"), e.get("detail"),
                             f"code={e['code']}" if e.get("code") else None] if p]
        if parts:
            return f"HTTP {sc}: " + " | ".join(parts)
    if body.get("status") and body.get("status") not in ("CAPTURED", "PENDING", "SUCCEEDED"):
        msg = body.get("message") or body.get("detail") or ""
        return f"HTTP {sc}: status={body.get('status')}" + (f" | {msg}" if msg else "")
    return f"HTTP {sc}: " + json.dumps(body)[:300]


def charge_card(card_id, amount, request_id, invoice_num, customer_name, access_token):
    payload = {"amount": f"{amount:.2f}", "currency": "USD", "capture": True,
               "cardOnFile": card_id, "context": {"mobile": False, "isEcommerce": True},
               "description": f"Invoice {invoice_num} - {customer_name}"}
    try:
        resp = requests.post(
            "https://api.intuit.com/quickbooks/v4/payments/charges",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Request-Id": request_id},
            json=payload, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        return {"classification": "uncertain", "error": f"network: {str(e)[:200]}",
                "request_id": request_id, "payment_type": "card"}
    classification = _classify_charge_response(resp, "card")
    base = {"classification": classification, "request_id": request_id, "payment_type": "card",
            "status_code": resp.status_code, "amount_requested": amount}
    body = None
    try:
        body = resp.json()
        base["raw_response"] = body
    except Exception:
        base["raw_text"] = resp.text[:500]
    if classification == "success" and body:
        return {**base, "charge_id": body.get("id"), "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode"), "status": body.get("status"),
                "card_last4": (body.get("card") or {}).get("number", "")[-4:],
                "card_type": (body.get("card") or {}).get("cardType")}
    return {**base, "error": extract_charge_error(resp, body)}


def charge_bank_account(bank_id, amount, request_id, invoice_num, customer_name, access_token):
    payload = {"amount": f"{amount:.2f}", "bankAccountOnFile": bank_id,
               "description": f"Invoice {invoice_num} - {customer_name}",
               "paymentMode": "WEB",
               "context": {"deviceInfo": {"macAddress": "", "ipAddress": "", "longitude": "",
                                          "latitude": "", "phoneNumber": ""}}}
    try:
        resp = requests.post(
            "https://api.intuit.com/quickbooks/v4/payments/echecks",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Request-Id": request_id},
            json=payload, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        return {"classification": "uncertain", "error": f"network: {str(e)[:200]}",
                "request_id": request_id, "payment_type": "ach"}
    classification = _classify_charge_response(resp, "ach")
    base = {"classification": classification, "request_id": request_id, "payment_type": "ach",
            "status_code": resp.status_code, "amount_requested": amount}
    body = None
    try:
        body = resp.json()
        base["raw_response"] = body
    except Exception:
        base["raw_text"] = resp.text[:500]
    if classification == "success" and body:
        return {**base, "charge_id": body.get("id"), "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode", ""), "status": body.get("status"),
                "card_last4": (body.get("bankAccount") or {}).get("accountNumber", "")[-4:],
                "card_type": "ACH"}
    return {**base, "error": extract_charge_error(resp, body)}


def record_qbo_payment(customer_id, invoice_id, amount, charge_result, invoice_num,
                       access_token, realm_id):
    """QBO Payment linked to the invoice, CCTransId = charge id (reconciliation)."""
    charge_id = charge_result.get("charge_id", "")
    pmt_method_id = (QBO_PMT_METHOD_ACH if charge_result.get("payment_type") == "ach"
                     else QBO_PMT_METHOD_CC)
    note = (f"Maint autopay | Inv# {invoice_num} | Charge ID: {charge_id} | "
            f"Auth: {charge_result.get('auth_code', '')} | "
            f"{charge_result.get('card_type', '')} x{charge_result.get('card_last4', '')} | "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
        json={"CustomerRef": {"value": customer_id}, "TotalAmt": amount,
              "PaymentMethodRef": {"value": pmt_method_id},
              "PaymentRefNum": invoice_num,
              "TxnDate": datetime.now().strftime("%Y-%m-%d"),
              "Line": [{"Amount": amount,
                        "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
              "PrivateNote": note,
              "CreditCardPayment": {
                  "CreditChargeInfo": {"ProcessPayment": True, "Amount": amount},
                  "CreditChargeResponse": {"Status": "Completed", "CCTransId": charge_id}},
              "TxnSource": "IntuitPayment"},
        timeout=60)
    if not resp.ok:
        return {"success": False, "error": resp.text[:400]}
    return {"success": True, "qbo_payment_id": resp.json().get("Payment", {}).get("Id")}


def send_receipt_then_invoice(payment_id, invoice_id, email, access_token, realm_id):
    """RECEIPT FIRST (payment/send), then the invoice copy (invoice/send)."""
    out = {"receipt": False, "invoice": False, "errors": []}
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json",
               "Content-Type": "application/octet-stream"}
    if not email:
        out["errors"].append("no email on file")
        return out
    if payment_id:
        r = requests.post(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{payment_id}/send?sendTo={email}",
            headers=headers, timeout=60)
        out["receipt"] = r.ok
        if not r.ok:
            out["errors"].append(f"receipt: HTTP {r.status_code} {r.text[:150]}")
    r = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}/send?sendTo={email}",
        headers=headers, timeout=60)
    out["invoice"] = r.ok
    if not r.ok:
        out["errors"].append(f"invoice: HTTP {r.status_code} {r.text[:150]}")
    return out


# ── the WAL (same table + semantics as the WO engine) ───────────────────────

def latest_attempt(cur, qbo_invoice_id):
    cur.execute(
        """SELECT * FROM billing.processing_attempts
           WHERE qbo_invoice_id = %s AND stage = 'maint' AND NOT dry_run
           ORDER BY attempted_at DESC LIMIT 1""",
        (qbo_invoice_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def create_attempt(conn, cur, invoice_number, qbo_invoice_id, channel, cpm_id,
                   charge_amount, dry_run):
    cur.execute(
        """INSERT INTO billing.processing_attempts
             (invoice_number, qbo_invoice_id, stage, status, idempotency_key,
              payment_method, charge_amount, dry_run, channel, customer_payment_method_id)
           VALUES (%s, %s, 'maint', 'pending', %s, %s, %s, %s, %s, %s)
           RETURNING *""",
        (invoice_number, qbo_invoice_id, str(uuid.uuid4()), channel, charge_amount,
         dry_run, channel, cpm_id))
    conn.commit()
    return dict(cur.fetchone())


def update_attempt(conn, cur, attempt_id, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE billing.processing_attempts SET {sets} WHERE id = %s",
                list(fields.values()) + [attempt_id])
    conn.commit()


def record_autopay_txn(conn, cur, p, charge_result, qbo_payment_id, emails, status):
    """The maintenance reporting record (Processing tab + projection gate)."""
    cur.execute(
        """INSERT INTO billing.autopay_transactions
             (billing_month, qbo_customer_id, customer_name, payment_method, card_type,
              last_four, charge_amount, charge_id, charge_status, charged_at,
              qbo_payment_id, qbo_invoice_ids, qbo_invoice_numbers,
              receipt_emailed, invoice_emailed, email_address, emailed_at,
              status, dry_run, verified, verified_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,now(),%s,false,true,now())""",
        (p["month_key"], p["qbo_customer_id"], p["customer_name"],
         charge_result.get("payment_type"), charge_result.get("card_type"),
         charge_result.get("card_last4"), charge_result.get("amount"),
         charge_result.get("charge_id"), charge_result.get("status"),
         qbo_payment_id, [p["qbo_invoice_id"]], [p["doc_number"]],
         emails.get("receipt", False), emails.get("invoice", False), p["email"], status))
    conn.commit()


LOAD_PERIOD = """
SELECT tbp.id, tbp.billing_month, tbp.qbo_customer_id, tbp.qbo_invoice_id,
       tbp.processing_status, tbp.locked_at,
       to_char(tbp.billing_month, 'YYYY-MM') AS month_key,
       c.display_name AS customer_name, c.email AS customer_email,
       i.doc_number, i.balance, i.email_status,
       ac.id AS autopay_id, ac.email AS autopay_email,
       pm.id AS cpm_id, pm.qbo_payment_method_id, pm.type AS pm_type,
       pm.is_active AS pm_active, pm.auto_disabled_at, pm.deactivated_at
FROM billing_audit.task_billing_periods tbp
LEFT JOIN public."Customers" c ON c.qbo_customer_id = tbp.qbo_customer_id
LEFT JOIN billing.invoices i ON i.qbo_invoice_id = tbp.qbo_invoice_id
LEFT JOIN billing.autopay_customers ac ON ac.id = tbp.autopay_customer_id AND ac.is_active
LEFT JOIN billing.customer_payment_methods pm ON pm.id = ac.payment_method_id
WHERE tbp.id = %s
"""


def process_one(conn, cur, period_id, access_token, realm_id, dry_run, force):
    cur.execute(LOAD_PERIOD, (period_id,))
    row = cur.fetchone()
    if not row:
        return {"period": period_id, "status": "error", "error": "period not found"}
    p = dict(row)
    p["email"] = p.get("autopay_email") or p.get("customer_email")

    # HARD GATE: only ready periods are chargeable (a flagged customer
    # structurally cannot reach this point)
    if p["processing_status"] != "ready_to_process" and not force:
        return {"period": period_id, "customer": p["customer_name"], "status": "skipped",
                "error": f"not ready_to_process (is {p['processing_status']})"}
    if p["locked_at"] is not None:
        return {"period": period_id, "customer": p["customer_name"], "status": "skipped",
                "error": "month locked"}
    if not p["qbo_invoice_id"] or p["balance"] is None:
        return {"period": period_id, "customer": p["customer_name"], "status": "skipped",
                "error": "no linked/cached invoice"}

    balance = float(p["balance"])
    invoice_num = p["doc_number"]
    on_autopay = p["autopay_id"] is not None
    pm_ok = (p["cpm_id"] is not None and p["pm_active"]
             and p["auto_disabled_at"] is None and p["deactivated_at"] is None)
    channel = ("ach" if on_autopay and pm_ok and "bank" in (p["pm_type"] or "").lower()
               else "card" if on_autopay and pm_ok else "email")

    # already settled -> just make sure the invoice went out
    if balance <= 0:
        emails = {"receipt": False, "invoice": p["email_status"] == "EmailSent"}
        if p["email_status"] != "EmailSent" and not dry_run:
            emails = send_receipt_then_invoice(None, p["qbo_invoice_id"], p["email"],
                                               access_token, realm_id)
            if emails["invoice"]:
                cur.execute("UPDATE billing.invoices SET email_status='EmailSent' WHERE qbo_invoice_id=%s",
                            (p["qbo_invoice_id"],))
                conn.commit()
        return {"period": period_id, "customer": p["customer_name"], "status": "already_paid",
                "invoice_sent": emails["invoice"]}

    # prior-attempt handling (idempotency: reuse the persisted key)
    prior = latest_attempt(cur, p["qbo_invoice_id"])
    if prior and prior["status"] == "succeeded":
        return {"period": period_id, "customer": p["customer_name"], "status": "skipped",
                "error": "already succeeded"}
    if prior and prior["status"] == "payment_orphan":
        return {"period": period_id, "customer": p["customer_name"], "status": "skipped",
                "error": "payment_orphan — human recovery required"}

    if channel == "email":
        # non-autopay: invoice email only, no charge
        if dry_run:
            return {"period": period_id, "customer": p["customer_name"], "status": "dry_run",
                    "plan": f"send invoice #{invoice_num} to {p['email']} (no autopay)"}
        attempt = create_attempt(conn, cur, invoice_num, p["qbo_invoice_id"], "email",
                                 None, balance, dry_run)
        emails = send_receipt_then_invoice(None, p["qbo_invoice_id"], p["email"],
                                           access_token, realm_id)
        ok = emails["invoice"]
        update_attempt(conn, cur, attempt["id"],
                       status="succeeded" if ok else "email_failed",
                       email_sent=ok, error_message=None if ok else "; ".join(emails["errors"]),
                       raw_result=_dumps(emails))
        if ok:
            cur.execute("UPDATE billing.invoices SET email_status='EmailSent' WHERE qbo_invoice_id=%s",
                        (p["qbo_invoice_id"],))
            conn.commit()
        return {"period": period_id, "customer": p["customer_name"],
                "status": "invoice_sent" if ok else "email_failed",
                "errors": emails["errors"] or None}

    # autopay charge path
    if dry_run:
        return {"period": period_id, "customer": p["customer_name"], "status": "dry_run",
                "plan": f"charge {channel} ····{(p['pm_type'] or '')} "
                        f"{balance:.2f} for invoice #{invoice_num}, receipt then invoice to {p['email']}"}

    if prior and prior["status"] == "charge_uncertain":
        attempt = prior  # REUSE the persisted idempotency key — Intuit dedupes
    elif prior and prior["status"] == "charge_succeeded":
        attempt = prior  # money moved; skip to record_payment
    else:
        attempt = create_attempt(conn, cur, invoice_num, p["qbo_invoice_id"], channel,
                                 p["cpm_id"], balance, dry_run)

    charge_result = None
    if attempt["status"] in ("pending", "charge_uncertain"):
        fn = charge_bank_account if channel == "ach" else charge_card
        charge_result = fn(p["qbo_payment_method_id"], balance, attempt["idempotency_key"],
                           invoice_num, p["customer_name"] or "", access_token)
        cls = charge_result["classification"]
        if cls == "uncertain":
            update_attempt(conn, cur, attempt["id"], status="charge_uncertain",
                           error_message=charge_result.get("error"),
                           raw_result=_dumps(charge_result))
            return {"period": period_id, "customer": p["customer_name"],
                    "status": "charge_uncertain", "error": charge_result.get("error")}
        if cls == "declined":
            update_attempt(conn, cur, attempt["id"], status="charge_declined",
                           error_message=charge_result.get("error"),
                           raw_result=_dumps(charge_result))
            cur.execute(
                """UPDATE billing.autopay_customers
                   SET consecutive_declines = consecutive_declines + 1,
                       payment_status = 'payment_issue', updated_at = now()
                   WHERE id = %s""", (p["autopay_id"],))
            conn.commit()
            return {"period": period_id, "customer": p["customer_name"],
                    "status": "charge_declined", "error": charge_result.get("error")}
        update_attempt(conn, cur, attempt["id"], status="charge_succeeded",
                       charge_id=charge_result.get("charge_id"),
                       raw_result=_dumps(charge_result))
    else:
        charge_result = {"charge_id": attempt["charge_id"], "payment_type": channel,
                         "amount": float(attempt["charge_amount"] or balance),
                         "card_type": None, "card_last4": None, "auth_code": "",
                         "status": "CAPTURED"}

    # record the QBO Payment (retry-safe: a repeat run reuses the charge_id)
    rec = record_qbo_payment(p["qbo_customer_id"], p["qbo_invoice_id"],
                             charge_result.get("amount", balance), charge_result,
                             invoice_num, access_token, realm_id)
    if not rec["success"]:
        update_attempt(conn, cur, attempt["id"], status="payment_orphan",
                       error_message=f"record_payment failed: {rec['error']}")
        return {"period": period_id, "customer": p["customer_name"],
                "status": "payment_orphan", "error": rec["error"]}
    update_attempt(conn, cur, attempt["id"], qbo_payment_id=rec["qbo_payment_id"])

    # RECEIPT first, then the invoice copy
    emails = send_receipt_then_invoice(rec["qbo_payment_id"], p["qbo_invoice_id"],
                                       p["email"], access_token, realm_id)
    final = "succeeded" if emails["invoice"] or emails["receipt"] else "email_failed"
    update_attempt(conn, cur, attempt["id"], status=final,
                   email_sent=emails["invoice"], raw_result=_dumps(emails))

    # reporting record + roster health + cache update (fires auto-promote)
    record_autopay_txn(conn, cur, p, charge_result, rec["qbo_payment_id"], emails,
                       "charge_success")
    cur.execute(
        """UPDATE billing.autopay_customers
           SET consecutive_declines = 0, payment_status = 'good', updated_at = now()
           WHERE id = %s""", (p["autopay_id"],))
    cur.execute(
        """UPDATE billing.invoices
           SET balance = 0, email_status = CASE WHEN %s THEN 'EmailSent' ELSE email_status END
           WHERE qbo_invoice_id = %s""",
        (emails["invoice"], p["qbo_invoice_id"]))
    conn.commit()
    cur.execute("SELECT billing_audit.project_maint_processing_status(%s, %s)",
                (p["billing_month"], p["qbo_customer_id"]))
    conn.commit()

    return {"period": period_id, "customer": p["customer_name"], "status": final,
            "charged": charge_result.get("amount"), "charge_id": charge_result.get("charge_id"),
            "qbo_payment_id": rec["qbo_payment_id"],
            "receipt_sent": emails["receipt"], "invoice_sent": emails["invoice"],
            "email_errors": emails["errors"] or None}


def main(period_ids: list = None,
         qbo_customer_ids: list = None,
         billing_month: str = None,
         dry_run: bool = True,
         force: bool = False):
    """period_ids: explicit periods; OR qbo_customer_ids + billing_month
    ('YYYY-MM'): all their ready periods that month."""
    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ids = list(period_ids or [])
        if qbo_customer_ids:
            if not billing_month:
                return {"status": "error", "error": "billing_month required with qbo_customer_ids"}
            cur.execute(
                """SELECT id FROM billing_audit.task_billing_periods
                   WHERE qbo_customer_id = ANY(%s) AND billing_month = %s
                     AND processing_status = 'ready_to_process' AND locked_at IS NULL""",
                (qbo_customer_ids, f"{billing_month}-01"))
            ids += [r["id"] for r in cur.fetchall()]
        if not ids:
            return {"status": "noop", "error": "no ready periods matched"}

        access_token, realm_id = refresh_qbo_token()
        results = [process_one(conn, cur, pid, access_token, realm_id, dry_run, force)
                   for pid in ids]
        by_status = {}
        for r in results:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {"dry_run": dry_run, "periods": len(ids), "by_status": by_status,
                "results": results}
    finally:
        conn.close()
