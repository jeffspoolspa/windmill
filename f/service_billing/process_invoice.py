# f/service_billing/process_invoice
#
# Charges cards / sends invoices for invoices in billing_status='ready_to_process'.
# Built on the write-ahead-log pattern to safely manage the dual-write problem
# (charge + ledger record can fail independently).
#
# State machine on billing.processing_attempts.status:
#   pending           -> row created, no external calls yet
#   charge_uncertain  -> charge call returned 5xx/timeout, money state unknown.
#                        Retry reuses idempotency_key (Intuit dedupes).
#   charge_declined   -> definitive failure, no money moved. Terminal.
#   charge_succeeded  -> charge_id received, record_payment not done yet.
#                        Retry skips charge step, retries only record_payment.
#   payment_orphan    -> charge succeeded but record_payment failed. HUMAN ONLY.
#                        Recover via recover_orphan=True after manual verification.
#   email_failed      -> money state ok, only email failed. Auto-retry email up to 3x.
#   succeeded         -> both charge + QBO Payment + emails done. Terminal.
#
# CRITICAL: idempotency_key is generated ONCE per attempt, persisted BEFORE the
# charge call, and reused on every retry. Intuit Payments uses Request-Id as its
# idempotency key — this is what prevents double-charges on crash recovery.

import requests
import wmill
import psycopg2
import psycopg2.extras
import json
import time
import uuid
from datetime import datetime, date

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

# QBO PaymentMethodRef IDs (must match the realm's QBO setup)
QBO_PMT_METHOD_CC = "21"
QBO_PMT_METHOD_ACH = "20"

# Email retry policy for payment_method='invoice' send-only path
EMAIL_RETRY_MAX = 3
EMAIL_RETRY_BACKOFF_S = 5


# =============================================================================
# QBO AUTH + HTTP HELPERS
# =============================================================================

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


def qbo_get(path, access_token, realm_id, params=None):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params=params, timeout=30,
    )


def qbo_post(path, access_token, realm_id, body):
    return requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )


def fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id):
    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)
    if not resp.ok:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return resp.json().get("Invoice"), None


def fetch_qbo_customer_email(customer_id, access_token, realm_id):
    resp = qbo_get(f"customer/{customer_id}", access_token, realm_id)
    if not resp.ok:
        return None
    customer = resp.json().get("Customer", {})
    return (customer.get("PrimaryEmailAddr") or {}).get("Address")


# =============================================================================
# DB CONNECTION + ATTEMPT-LOG HELPERS
# =============================================================================

def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def load_invoice(conn, qbo_invoice_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM billing.invoices WHERE qbo_invoice_id = %s", (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def load_linked_wo(conn, qbo_invoice_id):
    """Loads the WO matched to this invoice. wo_number is NOT NULL on processing_attempts."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM public.work_orders WHERE qbo_invoice_id = %s LIMIT 1",
                (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def latest_process_attempt(conn, qbo_invoice_id):
    """Most recent NON-dry-run process-stage attempt. Dry-runs are sandbox plans —
    they don't represent state and must not affect retry/resume decisions."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM billing.processing_attempts
        WHERE qbo_invoice_id = %s AND stage = 'process' AND dry_run = false
        ORDER BY attempted_at DESC
        LIMIT 1
    """, (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def create_attempt(conn, qbo_invoice_id, wo_number, invoice_number, payment_method,
                   charge_amount, dry_run, channel=None, customer_payment_method_id=None):
    """WRITE-AHEAD: insert pending attempt with fresh idempotency_key BEFORE any external call.

    channel / customer_payment_method_id are the new fields that supersede
    the legacy payment_method text. They're set up-front from the invoice's
    preferred_payment_type + target_payment_method_id, so the audit row
    knows what was attempted before the external call fires. Legacy
    payment_method is dual-written for the duration of the rollout.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO billing.processing_attempts (
            wo_number, invoice_number, qbo_invoice_id, stage, status,
            idempotency_key, payment_method, charge_amount, dry_run,
            channel, customer_payment_method_id
        ) VALUES (%s, %s, %s, 'process', 'pending', %s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (wo_number, invoice_number, qbo_invoice_id, str(uuid.uuid4()),
          payment_method, charge_amount, dry_run,
          channel, customer_payment_method_id))
    conn.commit()
    row = cur.fetchone(); cur.close()
    return dict(row)


def update_attempt(conn, attempt_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields.keys())
    vals = list(fields.values()) + [attempt_id]
    cur = conn.cursor()
    cur.execute(f"UPDATE billing.processing_attempts SET {sets} WHERE id = %s", vals)
    conn.commit(); cur.close()


def mark_invoice_processed(conn, qbo_invoice_id):
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET billing_status = 'processed', processed_at = now()
        WHERE qbo_invoice_id = %s
    """, (qbo_invoice_id,))
    conn.commit(); cur.close()


# How long we expect QBO webhooks to arrive after we make a write. If they
# don't show up within this window, cdc_reconciler will flip the expectation
# to 'missing' and surface in the UI for human investigation.
WEBHOOK_GRACE_MINUTES = 5


def insert_webhook_expectation(conn, entity_type, entity_id):
    """Record an expectation that QBO will send a webhook for this entity
    within the grace window. The webhook handler at /api/webhooks/qbo calls
    confirm_webhook_expectation(entity_type, entity_id) which matches by
    those two fields and flips status='pending' → 'confirmed'.

    Use this immediately after any QBO write whose effect we want to verify
    independently. It's optional — failure here doesn't fail the write
    (the webhook is the verification layer; this just lets us catch missed
    confirmations). Best-effort only.
    """
    if not entity_id:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO billing.webhook_expectations (
                entity_type, entity_id, expected_by, source, status
            ) VALUES (%s, %s, now() + (%s || ' minutes')::interval,
                      'self_initiated', 'pending')
        """, (entity_type, entity_id, str(WEBHOOK_GRACE_MINUTES)))
        conn.commit(); cur.close()
    except Exception as e:
        # Don't fail the write — log + continue. cdc_reconciler is the
        # safety net; missed expectation rows just mean we'd see the
        # change via webhook but no green-check confirmation in the UI.
        print(f"  (webhook_expectation insert warning [{entity_type}:{entity_id}]: {e})")


def refresh_invoice_cache(conn, qbo_invoice_id, qbo_invoice):
    """After charge + payment, refresh the cached balance/email_status so UI sees the new state."""
    def _subtotal(inv):
        for line in inv.get("Line", []) or []:
            if line.get("DetailType") == "SubTotalLineDetail":
                try:
                    return round(float(line.get("Amount", 0) or 0), 2)
                except (TypeError, ValueError):
                    pass
        total = float(inv.get("TotalAmt", 0) or 0)
        tax = float((inv.get("TxnTaxDetail") or {}).get("TotalTax", 0) or 0)
        return round(total - tax, 2)

    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET subtotal = %s, balance = %s, total_amt = %s,
            email_status = %s, raw = %s::jsonb, fetched_at = now()
        WHERE qbo_invoice_id = %s
    """, (
        _subtotal(qbo_invoice),
        float(qbo_invoice.get("Balance", 0) or 0),
        float(qbo_invoice.get("TotalAmt", 0) or 0),
        qbo_invoice.get("EmailStatus"),
        json.dumps(qbo_invoice),
        qbo_invoice_id,
    ))
    conn.commit(); cur.close()


# =============================================================================
# PAYMENT METHOD LOOKUP (live, from Intuit)
# =============================================================================

def load_applicable_credits(conn, qbo_customer_id):
    """Pre-charge safety net: return unapplied credits that COULD have been
    used but weren't. Excludes maintenance-scoped credits (memo matches
    'maint', case-insensitive) and anything older than 6 months (stale —
    typically already reconciled elsewhere or written off).

    Called right before we charge a card. If anything comes back, halt and
    push to needs_review so a human picks: apply the credit or override.
    This catches credits that landed between pre_process and process, or
    credits pre_process's matching rules didn't catch.
    """
    if not qbo_customer_id:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT qbo_payment_id, type, unapplied_amt, total_amt, txn_date, ref_num, memo
        FROM billing.customer_payments
        WHERE qbo_customer_id = %s
          AND unapplied_amt > 0
          AND (memo IS NULL OR memo !~* 'maint')
          AND (txn_date IS NULL OR txn_date >= (now() - interval '6 months')::date)
        ORDER BY txn_date DESC NULLS LAST
    """, (qbo_customer_id,))
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


def load_payment_method_by_id(conn, cpm_id):
    """Load a specific PM row by uuid. Used at charge time to look up the
    target_payment_method_id that pre_process_invoice picked.

    Returns the same shape as get_active_payment_method's result dict, OR
    a {has_method: False, error: ...} dict if the row is missing or not
    is_active. Caller treats both failure modes the same — surface to UI
    + flag the invoice for re-pre-processing (which will pick a fresh
    target_payment_method_id, or fall back to email if nothing's left).
    """
    if not cpm_id:
        return {"has_method": False, "error": "no target_payment_method_id set on invoice"}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, qbo_payment_method_id, type, card_brand, last_four,
               is_default, is_active, raw, auto_disabled_at, auto_disabled_reason
        FROM billing.customer_payment_methods
        WHERE id = %s
    """, (cpm_id,))
    row = cur.fetchone(); cur.close()
    if not row:
        return {"has_method": False, "error": f"target_payment_method_id {cpm_id} not found"}
    if not row.get("is_active"):
        # PM was deactivated between pre_process and process. Could be QBO
        # sync (customer removed the card) or the 3-strike trigger.
        reason = row.get("auto_disabled_reason") or "manually deactivated"
        return {"has_method": False,
                "error": f"target PM is no longer active ({reason})",
                "stale_cpm_id": str(row["id"])}
    return _pm_row_to_result(dict(row), picked_reason="invoice_target")


def get_active_payment_method(conn, customer_id, preferred_type=None):
    """Pick the payment instrument to charge, FROM THE DB cache.

    Reasons this is DB-side rather than live:
      - Every processing_attempt can then link customer_payment_method_id
        back to the exact row that was charged (audit + reconciliation).
      - The DB row is refreshed every 4h by pull_customer_payment_methods;
        we'd be reading the same Intuit state either way.
      - Keeps pre_process and process aligned on the same source of truth.

    ONLY considers QBO-flagged defaults (is_default = true). QBO scopes
    defaults per-type, so a customer can have at most one default card and
    one default ACH. We do NOT fall back to non-default methods on the
    theory that if QBO doesn't consider it the default, we shouldn't
    surprise-charge it.

    Picking rule:
      1. If preferred_type ('card' or 'ach') is given AND a default of that
         type exists, use it. This is the per-invoice override set from the
         detail page.
      2. Otherwise, pick the most-recently-added default across types.
         (Empirically 98%+ of customers' "default" IS their most recently
         added, so this matches both QBO semantics and user intuition.)

    Returns a dict with has_method, payment_type, method_id (QBO's),
    cpm_id (our DB uuid -- written to processing_attempts.customer_payment_method_id),
    and descriptive fields for logging. has_method=False on nothing-on-file.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. User override — try to satisfy preferred_type if it's a valid default.
    # Accept both new ('credit_card') and legacy ('card') values for backwards
    # compat. Normalize to the schema's current value before querying.
    if preferred_type in ("card", "credit_card", "ach"):
        normalized = "credit_card" if preferred_type in ("card", "credit_card") else "ach"
        cur.execute("""
            SELECT id, qbo_payment_method_id, type, card_brand, last_four,
                   is_default, raw
            FROM billing.customer_payment_methods
            WHERE qbo_customer_id = %s
              AND is_active = true
              AND is_default = true
              AND type = %s
            ORDER BY (raw->>'created') DESC NULLS LAST
            LIMIT 1
        """, (customer_id, normalized))
        row = cur.fetchone()
        if row:
            cur.close()
            return _pm_row_to_result(dict(row), picked_reason="user_override")

    # 2. Fallback — most recently added default of any type
    cur.execute("""
        SELECT id, qbo_payment_method_id, type, card_brand, last_four,
               is_default, raw
        FROM billing.customer_payment_methods
        WHERE qbo_customer_id = %s
          AND is_active = true
          AND is_default = true
        ORDER BY (raw->>'created') DESC NULLS LAST
        LIMIT 1
    """, (customer_id,))
    row = cur.fetchone(); cur.close()
    if not row:
        return {"has_method": False,
                "error": "No default card or bank account on file (DB cache)"}
    return _pm_row_to_result(dict(row), picked_reason="most_recent_default")


def _pm_row_to_result(row, picked_reason):
    raw = row.get("raw") or {}
    base = {
        "has_method": True,
        "payment_type": row["type"],   # 'credit_card' | 'ach' (post-rename schema)
        "method_id": row["qbo_payment_method_id"],
        "cpm_id": str(row["id"]),
        "last4": row.get("last_four"),
        "is_default": bool(row.get("is_default")),
        "picked_reason": picked_reason,
    }
    # Accept both 'credit_card' (new) and 'card' (legacy, in case a stale cpm
    # row still uses the old value during transition). Same payload either way.
    if row["type"] in ("credit_card", "card"):
        return {**base,
                "card_type": row.get("card_brand"),
                "exp_month": raw.get("expMonth"),
                "exp_year": raw.get("expYear")}
    return {**base, "bank_name": row.get("card_brand") or "Bank"}


# =============================================================================
# INTUIT PAYMENTS CHARGE (with idempotency_key + uncertain/definitive classification)
# =============================================================================

def _classify_charge_response(resp, payment_type):
    """Returns one of: 'success', 'declined', 'uncertain'.

    'declined' = 4xx with explicit error OR 200 with explicit failure status. No money moved.
    'uncertain' = 5xx, timeout, network error. Money state unknown — must retry with same key.
    'success' = 2xx with CAPTURED (card) or PENDING/SUCCEEDED (ACH).
    """
    if resp is None:
        return "uncertain"  # network/timeout exception
    sc = resp.status_code
    if sc >= 500:
        return "uncertain"
    if not resp.ok:
        # 4xx — definitive failure (auth, validation, declined card, etc.)
        return "declined"
    try:
        result = resp.json()
        status = (result.get("status") or "").upper()
        if payment_type == "card":
            return "success" if status == "CAPTURED" else "declined"
        else:  # ACH
            return "success" if status in ("PENDING", "SUCCEEDED") else "declined"
    except Exception:
        # 200 with unparseable body — treat as uncertain so we retry safely
        return "uncertain"


def extract_charge_error(resp, body=None):
    """Build the most useful human-readable error from a charge response.

    Intuit puts error info in different places depending on the failure mode:
      - Standard 4xx with errors array → errors[0].message + code + detail
      - 4xx with non-standard body → body.message or body.detail
      - 5xx with no body → "HTTP 502: <text fragment>"
      - 5xx with HTML body → "HTTP 503: text=..."
      - 200 with explicit failure status → "status=DECLINED" + any detail
      - Pre-classified body with no errors structure → fall back to dump
    Returns a string suitable for processing_attempts.error_message — never
    None if there's anything useful at all (which lets the UI always surface
    something instead of a blank).
    """
    if resp is None:
        return "no response from Intuit (network error)"

    # Try parsing body if not provided
    if body is None:
        try:
            body = resp.json()
        except Exception:
            body = None

    sc = resp.status_code

    # Body unparseable — fall back to raw text
    if body is None:
        text = (resp.text or "").strip()
        # HTML responses (gateway errors) are too long to dump verbatim
        if text.startswith("<") or "<html" in text[:200].lower():
            return f"HTTP {sc}: gateway returned HTML (likely 5xx upstream)"
        return f"HTTP {sc}: {text[:300] if text else 'empty body'}"

    # Standard Intuit errors array
    errors = body.get("errors") or []
    if errors:
        e = errors[0] if isinstance(errors[0], dict) else {}
        parts = []
        if e.get("message"):
            parts.append(e["message"])
        if e.get("detail") and e.get("detail") != e.get("message"):
            parts.append(e["detail"])
        if e.get("code"):
            parts.append(f"code={e['code']}")
        if e.get("moreInfo"):
            parts.append(f"info={e['moreInfo']}")
        if parts:
            return f"HTTP {sc}: " + " | ".join(parts)

    # Some failures put it on the top level (rare but observed)
    if body.get("status") and body.get("status") not in ("CAPTURED", "PENDING", "SUCCEEDED"):
        msg = body.get("message") or body.get("detail") or ""
        return f"HTTP {sc}: status={body.get('status')}" + (f" | {msg}" if msg else "")

    # Catch-all: dump a slice of the body
    return f"HTTP {sc}: " + json.dumps(body)[:300]


def charge_card(card_id, amount, request_id, invoice_num, customer_name, access_token):
    """Charge a stored card. request_id is the persisted idempotency key."""
    payload = {
        "amount": f"{amount:.2f}",
        "currency": "USD",
        "capture": True,
        "cardOnFile": card_id,
        "context": {"mobile": False, "isEcommerce": True},
        "description": f"Invoice {invoice_num} - {customer_name}",
    }
    try:
        resp = requests.post(
            "https://api.intuit.com/quickbooks/v4/payments/charges",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Request-Id": request_id},
            json=payload, timeout=30,
        )
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
        return {**base,
                "charge_id": body.get("id"),
                "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode"),
                "status": body.get("status"),
                "card_last4": (body.get("card") or {}).get("number", "")[-4:],
                "card_type": (body.get("card") or {}).get("cardType"),
                "created": body.get("created")}

    # Failure of any kind (declined, uncertain, or success-with-no-body) —
    # capture a useful error message regardless of body shape.
    return {**base, "error": extract_charge_error(resp, body)}


def charge_bank_account(bank_id, amount, request_id, invoice_num, customer_name, access_token):
    payload = {
        "amount": f"{amount:.2f}",
        "bankAccountOnFile": bank_id,
        "description": f"Invoice {invoice_num} - {customer_name}",
        "paymentMode": "WEB",
        "context": {"deviceInfo": {"macAddress": "", "ipAddress": "", "longitude": "",
                                   "latitude": "", "phoneNumber": ""}},
    }
    try:
        resp = requests.post(
            "https://api.intuit.com/quickbooks/v4/payments/echecks",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Request-Id": request_id},
            json=payload, timeout=30,
        )
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
        return {**base,
                "charge_id": body.get("id"),
                "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode", ""),
                "status": body.get("status"),
                "card_last4": (body.get("bankAccount") or {}).get("accountNumber", "")[-4:],
                "card_type": "ACH",
                "created": body.get("created")}

    return {**base, "error": extract_charge_error(resp, body)}


# =============================================================================
# QBO PAYMENT RECORD + INVOICE/RECEIPT EMAILS
# =============================================================================

def record_qbo_payment(customer_id, invoice_id, amount, charge_result, wo_num, invoice_num,
                        access_token, realm_id):
    """Create QBO Payment linked to invoice, with charge_id in CCTransId for reconciliation."""
    charge_id = charge_result.get("charge_id", "")
    auth_code = charge_result.get("auth_code", "")
    card_type = charge_result.get("card_type", "")
    card_last4 = charge_result.get("card_last4", "")
    pmt_method_id = (QBO_PMT_METHOD_ACH if charge_result.get("payment_type") == "ach"
                     else QBO_PMT_METHOD_CC)

    private_note = (f"Auto-charge | WO# {wo_num} | Inv# {invoice_num} | "
                    f"Charge ID: {charge_id} | Auth: {auth_code} | "
                    f"{card_type} x{card_last4} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    payment_data = {
        "CustomerRef": {"value": customer_id},
        "TotalAmt": amount,
        "PaymentMethodRef": {"value": pmt_method_id},
        "PaymentRefNum": wo_num,
        "TxnDate": datetime.now().strftime("%Y-%m-%d"),
        "Line": [{"Amount": amount,
                  "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
        "PrivateNote": private_note,
        "CreditCardPayment": {
            "CreditChargeInfo": {"ProcessPayment": True, "Amount": amount},
            "CreditChargeResponse": {"Status": "Completed", "CCTransId": charge_id},
        },
        "TxnSource": "IntuitPayment",
    }

    resp = qbo_post("payment", access_token, realm_id, payment_data)
    if not resp.ok:
        # QBO uses a different error envelope from Intuit Payments. Try the
        # standard QBO Fault structure first, then fall back to extract_charge_error.
        body = None
        try:
            body = resp.json()
        except Exception:
            pass
        err_msg = None
        if body:
            fault = (body.get("Fault") or {}).get("Error") or []
            if fault:
                f = fault[0] if isinstance(fault[0], dict) else {}
                parts = [
                    f.get("Message"),
                    f.get("Detail"),
                    f"code={f.get('code')}" if f.get("code") else None,
                ]
                err_msg = " | ".join(p for p in parts if p)
        if not err_msg:
            err_msg = extract_charge_error(resp, body)
        return {"success": False, "error": err_msg,
                "status_code": resp.status_code,
                "raw_response": body or resp.text[:500]}

    payment = resp.json().get("Payment", {})
    return {"success": True,
            "payment_id": payment.get("Id"),
            "payment_ref": payment.get("PaymentRefNum"),
            "total_amt": payment.get("TotalAmt")}


def send_invoice_email(invoice_id, customer_id, access_token, realm_id):
    """POST /invoice/{id}/send. If EmailStatus already EmailSent, skip."""
    inv_resp = qbo_get(f"invoice/{invoice_id}", access_token, realm_id)
    if inv_resp.ok:
        inv = inv_resp.json().get("Invoice", {})
        if inv.get("EmailStatus") == "EmailSent":
            return {"success": True, "skipped": True, "reason": "Already sent"}

    email = fetch_qbo_customer_email(customer_id, access_token, realm_id)
    send_url = f"invoice/{invoice_id}/send"
    if email:
        send_url += f"?sendTo={email}"

    resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{send_url}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    if not resp.ok:
        return {"success": False, "error": resp.text[:300], "email_attempted": email}
    return {"success": True, "sent_to": email}


def send_payment_receipt(payment_id, customer_id, access_token, realm_id):
    email = fetch_qbo_customer_email(customer_id, access_token, realm_id)
    if not email:
        return {"success": False, "error": "No customer email found"}

    resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{payment_id}/send?sendTo={email}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    if not resp.ok:
        return {"success": False, "error": resp.text[:300], "email_attempted": email}
    return {"success": True, "sent_to": email}


# =============================================================================
# CORE PROCESSING — single-invoice with state-machine recovery
# =============================================================================

def _result(qbo_invoice_id, status, **rest):
    return {"qbo_invoice_id": qbo_invoice_id, "status": status, **rest}


def process_one(conn, qbo_invoice_id, access_token, realm_id,
                dry_run=False, recover_orphan=False, force=False):
    """Main per-invoice flow. Returns dict with status + diagnostics."""
    invoice = load_invoice(conn, qbo_invoice_id)
    if not invoice:
        return _result(qbo_invoice_id, "error", error="invoice not found in billing.invoices")

    wo = load_linked_wo(conn, qbo_invoice_id)
    if not wo:
        return _result(qbo_invoice_id, "error", error="no linked work order — cannot process")
    wo_number = wo["wo_number"]
    invoice_number = invoice.get("doc_number")
    customer_id = invoice.get("qbo_customer_id")
    customer_name = invoice.get("customer_name") or ""

    # The route decision (charge vs email) lives on invoices.preferred_payment_type
    # now: 'email' → email path, 'ach'/'credit_card' → charge path.
    # Legacy invoices.payment_method ('on_file'/'invoice') is dual-written by
    # pre_process_invoice during the rollout for safety. We accept either,
    # preferring the new field. Once the legacy column is dropped, this falls
    # back to a hard error if preferred_payment_type is missing.
    preferred_type = invoice.get("preferred_payment_type")
    payment_method = invoice.get("payment_method")  # legacy, dual-written

    if preferred_type not in ("email", "ach", "credit_card"):
        # Fall back to legacy if new field is unset (e.g. very old invoice
        # that's never been re-pre-processed). Derive what we'd have set.
        if payment_method == "invoice":
            preferred_type = "email"
        elif payment_method == "on_file":
            # Can't tell ach vs credit_card from legacy alone; the target_payment_method_id
            # path will handle picking one. If both are NULL, we'll fail below.
            preferred_type = "credit_card"  # pessimistic default — picker will refine
        else:
            return _result(qbo_invoice_id, "error",
                           error=f"invalid preferred_payment_type '{preferred_type}' "
                                 f"and no legacy payment_method to fall back to "
                                 f"(re-run pre_process_invoice)")

    # Channel for this attempt mirrors the decision: 'email' for email path,
    # else the type that'll be charged. Stored on processing_attempts.channel
    # so audit queries don't need to JOIN to cpm.
    channel = preferred_type

    if invoice.get("billing_status") != "ready_to_process" and not (force or recover_orphan):
        return _result(qbo_invoice_id, "skipped",
                       reason=f"billing_status='{invoice.get('billing_status')}' (need ready_to_process or force=True)")

    # 1. PRE-FLIGHT: examine prior attempt
    prior = latest_process_attempt(conn, qbo_invoice_id)

    # Recover-orphan path: explicit human action. Requires status='payment_orphan' on prior.
    if recover_orphan:
        if not prior or prior["status"] != "payment_orphan":
            return _result(qbo_invoice_id, "error",
                           error=f"recover_orphan called but no payment_orphan attempt found "
                                 f"(prior status: {prior['status'] if prior else 'none'})")
        return _retry_record_payment_for_orphan(conn, prior, invoice, customer_id, customer_name,
                                                 wo_number, invoice_number, access_token, realm_id)

    # Auto-resume from charge_succeeded (charge landed, ledger write didn't)
    if prior and prior["status"] == "charge_succeeded" and not dry_run:
        return _retry_record_payment_for_orphan(conn, prior, invoice, customer_id, customer_name,
                                                 wo_number, invoice_number, access_token, realm_id)

    # Already done
    if prior and prior["status"] == "succeeded":
        # Verify QBO state aligns
        qbo_inv, err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if qbo_inv:
            refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv)
            if float(qbo_inv.get("Balance", 0) or 0) == 0:
                mark_invoice_processed(conn, qbo_invoice_id)
                return _result(qbo_invoice_id, "already_succeeded",
                               attempt_id=str(prior["id"]))
        return _result(qbo_invoice_id, "already_succeeded", attempt_id=str(prior["id"]),
                       note="prior succeeded but QBO state could not be verified")

    # Halt for human-required states
    if prior and prior["status"] == "payment_orphan":
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       charge_id=prior["charge_id"],
                       amount=float(prior["charge_amount"] or 0),
                       attempt_id=str(prior["id"]))

    if prior and prior["status"] == "charge_declined" and not force:
        # Only halt if the new attempt would do the SAME THING the declined
        # attempt did. The decline is specific to a (channel, PM) pair — if
        # the user has switched channels (e.g. credit_card → email) OR
        # picked a different PM (different card on the same channel), it's
        # a fresh attempt path, not a retry of the failed one.
        #
        # Practical example: prior attempt charged Visa-ending-1234 and was
        # declined. User edits the invoice to email-only and clicks Process.
        # We should email, not block on the prior card decline.
        new_target_pm_id = invoice.get("target_payment_method_id")
        new_target_pm_id_str = (
            str(new_target_pm_id) if new_target_pm_id else None
        )
        prior_pm_id_str = (
            str(prior.get("customer_payment_method_id"))
            if prior.get("customer_payment_method_id") else None
        )
        same_attempt_path = (
            prior.get("channel") == channel
            and prior_pm_id_str == new_target_pm_id_str
            and channel != "email"  # email path always safe to re-attempt
        )
        if same_attempt_path:
            return _result(qbo_invoice_id, "needs_human", reason="charge_declined",
                           error=prior.get("error_message"),
                           attempt_id=str(prior["id"]),
                           note="prior attempt declined this same PM; "
                                "change channel/PM or pass force=true to retry")

    # Reconciler couldn't determine charge state — human investigation required.
    # Force=True bypasses this (admin override after manual verification).
    if prior and prior["status"] == "needs_reconcile_review" and not force:
        return _result(qbo_invoice_id, "needs_human", reason="needs_reconcile_review",
                       error=prior.get("error_message"),
                       attempt_id=str(prior["id"]))

    # 2. Refresh QBO state — may have been paid/sent externally
    qbo_inv, err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if not qbo_inv:
        return _result(qbo_invoice_id, "error", error=f"qbo_fetch_failed: {err}")
    refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv)

    qbo_balance = float(qbo_inv.get("Balance", 0) or 0)
    qbo_email_sent = qbo_inv.get("EmailStatus") == "EmailSent"

    # If invoice fully paid externally AND email sent, nothing to do
    if qbo_balance == 0 and qbo_email_sent:
        mark_invoice_processed(conn, qbo_invoice_id)
        return _result(qbo_invoice_id, "already_paid_and_sent")

    # 3. Reuse existing pending/uncertain attempt (preserves idempotency_key)
    #    OR create new with a fresh key. Three policies based on prior status:
    #
    #    - 'pending'                    → reuse. No external call has fired,
    #                                     so the same key is safe.
    #
    #    - 'charge_uncertain' (<24h old) → reuse. Within Intuit's idempotency
    #                                     window; reusing the same key returns
    #                                     the cached response (or processes
    #                                     fresh if the original timed out
    #                                     before reaching Intuit). Either way
    #                                     no double-charge possible.
    #
    #    - 'charge_uncertain' (>24h old) → AUTO-PROMOTE to expired + create
    #                                     fresh attempt. Intuit's cache has
    #                                     expired so the same key would be
    #                                     treated as new anyway. But before
    #                                     we issue a NEW key, we ideally want
    #                                     reconcile_payments to confirm no
    #                                     charge landed. If reconciler has
    #                                     run, status will already be
    #                                     'charge_uncertain_expired' (see
    #                                     below). If reconciler hasn't run
    #                                     yet, fall through with caution —
    #                                     log + create new attempt anyway.
    #
    #    - 'charge_uncertain_expired'   → reconciler verified no charge.
    #                                     Create fresh attempt with new key.
    #
    #    Anything else (succeeded, declined, payment_orphan, etc) was
    #    handled in earlier branches — fall through to fresh attempt.
    target_pm_id = invoice.get("target_payment_method_id")

    def _create_fresh():
        return create_attempt(
            conn, qbo_invoice_id, wo_number, invoice_number,
            payment_method, qbo_balance, dry_run,
            channel=channel,
            customer_payment_method_id=str(target_pm_id) if target_pm_id else None,
        )

    if prior and prior["status"] == "pending":
        attempt = prior
    elif prior and prior["status"] == "charge_uncertain":
        # Within idempotency window vs expired — make the call here.
        attempted_at = prior.get("attempted_at")
        from datetime import datetime, timezone, timedelta
        if attempted_at and attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - attempted_at) if attempted_at else timedelta()
        if age > timedelta(hours=24):
            # Idempotency window expired. Mark old attempt as expired and
            # create a fresh one. Note: reconcile_payments would normally
            # promote this status itself + verify no charge landed; if we're
            # here it means reconciler hasn't caught up yet, so we proceed
            # cautiously. The fresh attempt's new key avoids any cached
            # response and the worst case is a *missing* charge (not double).
            cur = conn.cursor()
            cur.execute("""
                UPDATE billing.processing_attempts
                SET status='charge_uncertain_expired',
                    error_message=COALESCE(error_message, '')
                                  || ' | manually expired after 24h by process_invoice'
                WHERE id = %s
            """, (prior["id"],))
            conn.commit(); cur.close()
            attempt = _create_fresh()
        else:
            attempt = prior
    elif prior and prior["status"] == "charge_uncertain_expired":
        # Reconciler verified no charge — safe to retry with fresh key.
        attempt = _create_fresh()
    else:
        attempt = _create_fresh()

    # 4. DRY-RUN short-circuit
    if dry_run:
        plan = _build_dry_run_plan(payment_method, qbo_balance, qbo_email_sent,
                                    customer_id, conn, attempt,
                                    preferred_type=preferred_type,
                                    target_pm_id=invoice.get("target_payment_method_id"))
        # Tie the dry-run attempt to the exact payment method row that WOULD
        # have been charged, so the audit trail mirrors live runs.
        pm_on_file = plan.get("payment_method_on_file") or {}
        cpm_id = pm_on_file.get("cpm_id")
        update_attempt(conn, attempt["id"], status="succeeded",
                        raw_result=json.dumps(plan),
                        customer_payment_method_id=cpm_id)
        return _result(qbo_invoice_id, "dry_run_complete",
                       attempt_id=str(attempt["id"]),
                       plan=plan)

    # 5. ROUTE — based on the new preferred_payment_type. Wrapped in a
    # safety-net try/except so any uncaught exception doesn't leave the
    # write-ahead attempt row stuck at status='pending' forever. We flip
    # to 'charge_uncertain' on crash because we don't know whether the
    # external call (charge or email) actually fired or not — the
    # reconciler will resolve charges; email path is no-op safe (its
    # internal retries handle their own state, so reaching this except
    # means something deeper crashed).
    try:
        if preferred_type == "email":
            return _process_invoice_only(conn, attempt, invoice, qbo_inv, customer_id,
                                          access_token, realm_id)
        # preferred_type IN ('ach', 'credit_card') → charge path
        return _process_charge_path(conn, attempt, invoice, qbo_inv, customer_id, customer_name,
                                     wo_number, invoice_number, qbo_balance, access_token, realm_id)
    except Exception as e:
        # Don't leave a pending row orphaned. Mark as charge_uncertain so
        # reconcile_payments queries Intuit and resolves it on next tick.
        try:
            update_attempt(
                conn, attempt["id"],
                status="charge_uncertain",
                error_message=f"process_one crashed mid-flight: {str(e)[:300]}",
            )
        except Exception as inner:
            print(f"  WARN: failed to mark attempt charge_uncertain: {inner}")
        # Re-raise so the bulk loop's except still captures it for stats.
        raise


def _build_dry_run_plan(payment_method, balance, email_already_sent, customer_id,
                         conn, attempt, preferred_type=None, target_pm_id=None):
    """Predicts what a live run would do without making external calls.

    target_pm_id (when present) is the invoices.target_payment_method_id
    that pre_process_invoice picked. We load it directly instead of
    re-running the picker — same source of truth as the live path.
    Falls back to get_active_payment_method only when target_pm_id is
    missing (legacy invoice that hasn't been re-pre-processed).
    """
    is_charge = preferred_type in ("ach", "credit_card") if preferred_type else (payment_method == "on_file")
    plan = {
        "payment_method": payment_method,
        "preferred_payment_type": preferred_type,
        "amount_to_charge": balance if is_charge and balance > 0 else 0,
        "would_send_invoice_email": not email_already_sent,
        "would_send_receipt": is_charge and balance > 0,
        "idempotency_key": attempt["idempotency_key"],
    }
    if is_charge and balance > 0:
        # Mirror the live halts in the plan so dry-run accurately predicts
        # what WOULD happen — surfaces credit-check blocks and missing
        # payment methods without actually charging.
        remaining_credits = load_applicable_credits(conn, customer_id)
        if remaining_credits:
            total = sum(float(c.get("unapplied_amt") or 0) for c in remaining_credits)
            plan["would_halt"] = "credits_available"
            plan["credits_found"] = [
                {"qbo_payment_id": c.get("qbo_payment_id"),
                 "unapplied_amt": float(c.get("unapplied_amt") or 0),
                 "txn_date": str(c.get("txn_date")) if c.get("txn_date") else None,
                 "memo": c.get("memo")}
                for c in remaining_credits
            ]
            plan["credits_total_unapplied"] = total

        # Use the target_payment_method_id pre_process picked if present.
        # That's the row the live path will charge — load it as-is so the
        # dry-run reflects reality.
        if target_pm_id:
            pm = load_payment_method_by_id(conn, str(target_pm_id))
        else:
            # Legacy fallback: invoice never had target_payment_method_id set.
            # Use the picker with whatever preferred_type we have.
            pm = get_active_payment_method(conn, customer_id,
                                            preferred_type=preferred_type)
        plan["payment_method_on_file"] = pm
        if not pm.get("has_method"):
            plan["would_fail"] = pm.get("error") or "no_payment_method"
    return plan


def _process_charge_path(conn, attempt, invoice, qbo_inv, customer_id, customer_name,
                          wo_number, invoice_number, balance, access_token, realm_id):
    qbo_invoice_id = invoice["qbo_invoice_id"]

    # If balance is 0 (covered by credits in pre_process), skip charge — just send invoice email + mark done
    if balance == 0:
        email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
        update_attempt(conn, attempt["id"], email_sent=email["success"],
                        raw_result=json.dumps({"email": email, "skipped_charge_zero_balance": True}))
        if not email["success"] and not email.get("skipped"):
            update_attempt(conn, attempt["id"], status="email_failed",
                            error_message=email.get("error"))
            return _result(qbo_invoice_id, "email_failed",
                           attempt_id=str(attempt["id"]), error=email.get("error"))
        update_attempt(conn, attempt["id"], status="succeeded")
        mark_invoice_processed(conn, qbo_invoice_id)
        return _result(qbo_invoice_id, "succeeded",
                       attempt_id=str(attempt["id"]),
                       note="balance was zero — sent invoice only")

    # Credit re-check — catches credits that landed between pre_process and
    # process (new payment from customer, credit memo just issued, etc.) or
    # anything pre_process's matching rules missed. Excludes maintenance
    # credits + stale credits (>6 months) which are typically irrelevant.
    # If any applicable credit exists, halt and return the invoice to
    # needs_review so a human decides: apply it or charge through.
    remaining_credits = load_applicable_credits(conn, customer_id)
    if remaining_credits:
        total_unapplied = sum(float(c.get("unapplied_amt") or 0) for c in remaining_credits)
        reason = f"credits_available ({len(remaining_credits)} credit(s), ${total_unapplied:.2f} unapplied)"
        update_attempt(conn, attempt["id"], status="charge_declined",
                        error_message=reason,
                        charge_result=json.dumps({"credits_found": remaining_credits}))
        rb_cur = conn.cursor()
        rb_cur.execute("""
            UPDATE billing.invoices
            SET billing_status = 'needs_review', needs_review_reason = %s
            WHERE qbo_invoice_id = %s
        """, (reason, qbo_invoice_id))
        conn.commit(); rb_cur.close()
        return _result(qbo_invoice_id, "needs_human", reason="credits_available",
                       attempt_id=str(attempt["id"]),
                       error=reason,
                       credits_found=len(remaining_credits),
                       total_unapplied=total_unapplied)

    # Load the payment method that pre_process_invoice picked. We do NOT
    # re-pick at charge time — the decision was made at pre-process time
    # so it stays stable across the user's UI session, and any per-invoice
    # type override (set via the UI) is preserved by reading the stored
    # target_payment_method_id rather than re-running the picker.
    #
    # If the target is missing (legacy invoice that never got pre-processed
    # under the new model) OR no longer active (PM removed in QBO between
    # pre-process and process, or auto-disabled by the 3-strike trigger),
    # we surface the failure clearly and tell the user to re-run pre-process.
    target_pm_id = invoice.get("target_payment_method_id")
    if not target_pm_id:
        # Legacy fallback: invoice has no target set (very old). Use the picker
        # once, just for backwards compat. Drops out when legacy column dies.
        pm = get_active_payment_method(conn, customer_id,
                                        preferred_type=invoice.get("preferred_payment_type"))
    else:
        pm = load_payment_method_by_id(conn, str(target_pm_id))

    if not pm.get("has_method"):
        update_attempt(conn, attempt["id"], status="charge_declined",
                        error_message=pm.get("error", "no payment method"),
                        charge_result=json.dumps(pm))
        return _result(qbo_invoice_id, "needs_human", reason="no_payment_method",
                       attempt_id=str(attempt["id"]),
                       error=pm.get("error"))

    # Pin the attempt to the chosen payment method NOW — before we fire any
    # external calls. Idempotency_key + cpm_id together form the full audit
    # trail even if the charge request fails or the row is later deactivated.
    # (For the target_payment_method_id path this is usually redundant since
    # create_attempt set it up-front, but legacy fallback path needs it.)
    update_attempt(conn, attempt["id"], customer_payment_method_id=pm["cpm_id"])

    # CHARGE — pass attempt.idempotency_key as Request-Id (this is what makes retry safe).
    # Accept both 'credit_card' (post-rename) and 'card' (legacy in-flight rows).
    if pm["payment_type"] in ("credit_card", "card"):
        cr = charge_card(pm["method_id"], balance, attempt["idempotency_key"],
                          invoice_number, customer_name, access_token)
    else:
        cr = charge_bank_account(pm["method_id"], balance, attempt["idempotency_key"],
                                  invoice_number, customer_name, access_token)

    classification = cr.get("classification")

    if classification == "uncertain":
        # Money state genuinely unknown. Persist + halt; will be resolved by reconcile_payments
        # or by a manual re-run (which will reuse the same idempotency_key).
        update_attempt(conn, attempt["id"], status="charge_uncertain",
                        charge_result=json.dumps(cr),
                        error_message=cr.get("error"))
        return _result(qbo_invoice_id, "uncertain",
                       attempt_id=str(attempt["id"]),
                       error=cr.get("error"),
                       note="charge state unknown — reconcile_payments will resolve, or retry safely (idempotency_key reused)")

    if classification == "declined":
        update_attempt(conn, attempt["id"], status="charge_declined",
                        charge_result=json.dumps(cr),
                        error_message=cr.get("error"))
        return _result(qbo_invoice_id, "needs_human", reason="charge_declined",
                       attempt_id=str(attempt["id"]),
                       error=cr.get("error"))

    # CHARGE SUCCEEDED — persist charge_id IMMEDIATELY before attempting record_payment
    update_attempt(conn, attempt["id"], status="charge_succeeded",
                    charge_id=cr["charge_id"],
                    charge_result=json.dumps(cr))

    # Record payment in QBO
    pay = record_qbo_payment(customer_id, qbo_invoice_id, balance, cr,
                              wo_number, invoice_number, access_token, realm_id)

    if not pay["success"]:
        # DANGER: money moved, ledger didn't. Halt + flag for human.
        update_attempt(conn, attempt["id"], status="payment_orphan",
                        error_message=f"record_payment failed: {pay.get('error', '')[:300]}")
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       attempt_id=str(attempt["id"]),
                       charge_id=cr["charge_id"],
                       amount=balance,
                       error=pay.get("error"))

    update_attempt(conn, attempt["id"], qbo_payment_id=pay["payment_id"])

    # Webhook confirmation: QBO fires Payment.Create for the Payment we made.
    # /api/webhooks/qbo's confirm_webhook_expectation('Payment', payment_id)
    # matches this row and flips status='confirmed'. If it never arrives,
    # cdc_reconciler flips it to 'missing' — that's our signal that QBO
    # didn't actually commit despite returning 200.
    insert_webhook_expectation(conn, "Payment", pay["payment_id"])

    # NOTE: We deliberately do NOT insert an Invoice expectation here even
    # though the invoice's balance changed. QBO does NOT fire Invoice.Update
    # webhooks for balance changes driven by Payment application — only for
    # direct PATCHes on the invoice (memo, due date, etc.). Empirically
    # observed: every Invoice expectation we used to insert here went to
    # 'missing'. The Payment.Create webhook is sufficient — refresh_payment
    # updates the invoice cache as a side effect, and the auto-promote
    # trigger flips billing_status to processed.

    # Send receipt (best-effort — financial state already correct)
    receipt = send_payment_receipt(pay["payment_id"], customer_id, access_token, realm_id)
    update_attempt(conn, attempt["id"], email_sent=receipt["success"])

    # Refresh cached balance
    fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if fresh:
        refresh_invoice_cache(conn, qbo_invoice_id, fresh)

    update_attempt(conn, attempt["id"], status="succeeded",
                    raw_result=json.dumps({"payment": pay, "receipt": receipt}))
    mark_invoice_processed(conn, qbo_invoice_id)
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=str(attempt["id"]),
                   charge_id=cr["charge_id"],
                   qbo_payment_id=pay["payment_id"],
                   receipt_sent=receipt["success"])


def _retry_record_payment_for_orphan(conn, prior, invoice, customer_id, customer_name,
                                      wo_number, invoice_number, access_token, realm_id):
    """Resume from charge_succeeded or payment_orphan: try record_payment again with persisted charge_id.
    Does NOT charge again. Idempotency_key is reused via the charge_id (already in QBO Intuit Payments)."""
    qbo_invoice_id = invoice["qbo_invoice_id"]
    charge_result = prior.get("charge_result") or {}
    if isinstance(charge_result, str):
        charge_result = json.loads(charge_result)

    charge_id = prior.get("charge_id") or charge_result.get("charge_id")
    if not charge_id:
        return _result(qbo_invoice_id, "error",
                       error="orphan recovery requested but no charge_id on prior attempt",
                       attempt_id=str(prior["id"]))

    amount = float(prior["charge_amount"] or 0)
    pay = record_qbo_payment(customer_id, qbo_invoice_id, amount, charge_result,
                              wo_number, invoice_number, access_token, realm_id)

    if not pay["success"]:
        update_attempt(conn, prior["id"], status="payment_orphan",
                        error_message=f"orphan recovery: record_payment still failing: {pay.get('error', '')[:300]}")
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       attempt_id=str(prior["id"]),
                       charge_id=charge_id, amount=amount,
                       error=pay.get("error"),
                       note="record_payment retry failed — verify in QBO/Intuit")

    update_attempt(conn, prior["id"], qbo_payment_id=pay["payment_id"])

    # Same webhook-confirmation pattern as the primary charge path:
    # Payment.Create fires; Invoice.Update for balance change does not
    # (so we don't insert an Invoice expectation here either).
    insert_webhook_expectation(conn, "Payment", pay["payment_id"])

    receipt = send_payment_receipt(pay["payment_id"], customer_id, access_token, realm_id)
    update_attempt(conn, prior["id"], email_sent=receipt["success"])

    fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if fresh:
        refresh_invoice_cache(conn, qbo_invoice_id, fresh)

    update_attempt(conn, prior["id"], status="succeeded",
                    raw_result=json.dumps({"orphan_recovery": True, "payment": pay,
                                            "receipt": receipt}))
    mark_invoice_processed(conn, qbo_invoice_id)
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=str(prior["id"]),
                   charge_id=charge_id,
                   qbo_payment_id=pay["payment_id"],
                   recovered_from="orphan_or_charge_succeeded")


def _process_invoice_only(conn, attempt, invoice, qbo_inv, customer_id, access_token, realm_id):
    """preferred_payment_type='email' — email IS the deliverable. Auto-retry email up to N times."""
    qbo_invoice_id = invoice["qbo_invoice_id"]
    last_err = None
    for i in range(EMAIL_RETRY_MAX):
        email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
        if email["success"]:
            update_attempt(conn, attempt["id"], status="succeeded", email_sent=True,
                            raw_result=json.dumps({"email": email, "attempts": i + 1}))
            # Webhook confirmation: QBO fires Invoice.Emailed when send succeeds.
            # If 'skipped' (EmailStatus was already EmailSent), the webhook
            # already happened — no need to expect another.
            if not email.get("skipped"):
                insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
            mark_invoice_processed(conn, qbo_invoice_id)
            fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
            if fresh:
                refresh_invoice_cache(conn, qbo_invoice_id, fresh)
            return _result(qbo_invoice_id, "succeeded",
                           attempt_id=str(attempt["id"]),
                           sent_to=email.get("sent_to"),
                           skipped=email.get("skipped", False))
        last_err = email.get("error")
        if i + 1 < EMAIL_RETRY_MAX:
            time.sleep(EMAIL_RETRY_BACKOFF_S)

    update_attempt(conn, attempt["id"], status="email_failed",
                    error_message=last_err,
                    raw_result=json.dumps({"attempts": EMAIL_RETRY_MAX, "last_error": last_err}))
    return _result(qbo_invoice_id, "email_failed",
                   attempt_id=str(attempt["id"]),
                   error=last_err)


# =============================================================================
# MAIN
# =============================================================================

def main(qbo_invoice_id: str = None,
         qbo_invoice_ids: list = None,
         dry_run: bool = False,
         recover_orphan: bool = False,
         force: bool = False,
         bulk_all: bool = False,
         limit: int = None,
         sleep_ms: int = 800):
    """
    Modes:
      - Single: pass qbo_invoice_id
      - List: pass qbo_invoice_ids=[...]  (used by Process Selected button)
      - Bulk-all: pass bulk_all=True (processes everything in ready_to_process)

    Flags:
      - dry_run=True: log what would happen, NO external API calls. Writes attempt row with dry_run=true.
      - recover_orphan=True: requires qbo_invoice_id + prior status='payment_orphan'. Retries record_payment with persisted charge_id.
      - force=True: bypass billing_status='ready_to_process' guard (e.g. retry charge_declined invoices)
    """
    if not qbo_invoice_id and not qbo_invoice_ids and not bulk_all:
        return {"status": "error", "error": "pass qbo_invoice_id, qbo_invoice_ids=[...], or bulk_all=True"}

    print(f"=== process_invoice (dry_run={dry_run}, recover_orphan={recover_orphan}, "
          f"force={force}, bulk_all={bulk_all}) ===")

    conn = get_db_conn()
    try:
        access_token, realm_id = refresh_qbo_token()

        # Single mode
        if qbo_invoice_id and not qbo_invoice_ids:
            return process_one(conn, qbo_invoice_id, access_token, realm_id,
                                dry_run=dry_run, recover_orphan=recover_orphan, force=force)

        # Determine target list
        if qbo_invoice_ids:
            targets = list(qbo_invoice_ids)
        else:  # bulk_all
            cur = conn.cursor()
            sql = ("SELECT qbo_invoice_id FROM billing.invoices "
                   "WHERE billing_status = 'ready_to_process' "
                   "ORDER BY txn_date DESC NULLS LAST")
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            targets = [r[0] for r in cur.fetchall()]
            cur.close()

        print(f"Processing {len(targets)} invoice(s)")
        stats = {"succeeded": 0, "needs_human": 0, "uncertain": 0, "email_failed": 0,
                 "already_succeeded": 0, "already_paid_and_sent": 0,
                 "skipped": 0, "error": 0, "dry_run_complete": 0}
        sample = []

        for i, qid in enumerate(targets):
            try:
                res = process_one(conn, qid, access_token, realm_id,
                                   dry_run=dry_run, recover_orphan=recover_orphan, force=force)
            except Exception as e:
                res = _result(qid, "error", error=str(e)[:300])

            status = res.get("status", "error")
            stats[status] = stats.get(status, 0) + 1

            if i < 20:
                sample.append(res)

            print(f"  [{i+1}/{len(targets)}] {qid} -> {status}"
                  + (f"  ({res.get('reason') or res.get('error') or ''})" if status not in ('succeeded', 'dry_run_complete') else ''))

            if sleep_ms and i + 1 < len(targets):
                time.sleep(sleep_ms / 1000.0)

        print(f"=== done: {stats} ===")
        return {"status": "success", "total": len(targets), "stats": stats, "sample": sample,
                "dry_run": dry_run}

    finally:
        conn.close()
