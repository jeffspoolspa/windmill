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
from datetime import datetime, date, timezone
from decimal import Decimal


def _json_default(o):
    """Fallback for json.dumps when we're serializing DB rows. psycopg2
    returns NUMERIC as Decimal and DATE/TIMESTAMP as date/datetime, none of
    which json.dumps handles out of the box. Bug that stranded MCDONALD and
    OBRIEN attempts in 'pending' when credit-recheck tried to serialize their
    unapplied_amt Decimals at _dumps({'credits_found': remaining_credits})."""
    if isinstance(o, Decimal):
        # Amounts are always money values — float is sufficient, and matches
        # how we deserialize elsewhere (float(row['unapplied_amt']))
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} not JSON serializable")


def _dumps(obj):
    """json.dumps with our DB-friendly default. Use everywhere we serialize
    anything that might contain Decimal / date / datetime."""
    return json.dumps(obj, default=_json_default)

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


# Freshness budget — how old a data source can be before we refresh it at
# preflight time. Chosen so the first invoice in a batch pays the refresh
# cost once, then all subsequent invoices in the same run see fresh data.
# Tighter than the 4h schedule so we don't coast on stale reads.
FRESHNESS_BUDGET_MINUTES = 30


def _freshness_gap_minutes(fetched_at):
    """Minutes since fetched_at, or infinity if null/naive."""
    if not fetched_at:
        return float("inf")
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60.0


def preflight_refresh_if_stale(conn, target_customer_ids=None):
    """Check how fresh each data source is and fire a refresh if stale.
    Called ONCE per process_invoice invocation, before the loop — first
    invoice in a batch pays the cost, every subsequent invoice sees fresh
    data. Returns a dict of what was refreshed for the result summary.

    Only refreshes sources that matter for charging correctness:
      - customer_payment_methods (which card/ACH we charge)
      - customer_payments       (credits that might cover the invoice)

    billing.invoices freshness is irrelevant here because process_one
    does a LIVE per-invoice QBO fetch before charging.

    target_customer_ids: when provided (batch + bulk_all flows), we scope
    the payment-method refresh to just these customers — bypasses the
    pull script's internal 4h TTL so the refresh is guaranteed to touch
    every customer we're about to charge, not just the ones the TTL
    decides are due.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT "
        "(SELECT MAX(fetched_at) FROM billing.customer_payment_methods),"
        "(SELECT MAX(fetched_at) FROM billing.customer_payments)"
    )
    pm_last, pay_last = cur.fetchone()
    cur.close()

    pm_gap = _freshness_gap_minutes(pm_last)
    pay_gap = _freshness_gap_minutes(pay_last)

    refreshed = []
    failed = []

    if pm_gap > FRESHNESS_BUDGET_MINUTES:
        # Scope the refresh. If we know exactly which customers we're
        # going to touch, pass their IDs (bypasses the pull's 4h TTL).
        # Otherwise fall back to force_refresh so every customer gets a
        # fresh pull regardless of TTL.
        if target_customer_ids:
            args = {"customer_ids": [str(c) for c in target_customer_ids if c]}
            scope = f"{len(args['customer_ids'])} customers"
        else:
            args = {"force_refresh": True}
            scope = "all customers"
        print(f"  preflight: payment methods {pm_gap:.1f}min old — refreshing {scope}")
        try:
            # Use run_script_by_path (non-deprecated). run_script_sync was
            # sending requests to the /jobs/run/h/ (hash) endpoint instead of
            # /jobs/run/p/ (path), which 404'd.
            wmill.run_script_by_path(
                "f/service_billing/pull_customer_payment_methods",
                args=args, timeout=300,
            )
            refreshed.append(f"payment_methods ({scope}, was {pm_gap:.0f}min stale)")
        except Exception as e:
            print(f"  preflight: pull_customer_payment_methods FAILED: {e}")
            failed.append({"source": "payment_methods", "age_min": pm_gap, "error": str(e)[:200]})
            # Don't abort — per-invoice pre-charge credit recheck + decline
            # handling on revoked-card errors are the secondary defenses.
            # But surface this loudly in the result so reviewers see it.

    if pay_gap > FRESHNESS_BUDGET_MINUTES:
        print(f"  preflight: customer payments {pay_gap:.1f}min old — refreshing")
        try:
            wmill.run_script_by_path(
                "f/service_billing/pull_qbo_credits",
                args={"lookback_days": 180}, timeout=300,
            )
            refreshed.append(f"customer_payments (was {pay_gap:.0f}min stale)")
        except Exception as e:
            print(f"  preflight: pull_qbo_credits FAILED: {e}")
            failed.append({"source": "customer_payments", "age_min": pay_gap, "error": str(e)[:200]})

    if not refreshed and not failed:
        print(f"  preflight: all data within {FRESHNESS_BUDGET_MINUTES}min — no refresh needed")
    elif failed:
        print(f"  preflight: WARNING — {len(failed)} refresh(es) failed. "
              f"Proceeding with per-invoice live checks as safety net.")

    # Return both successes and failures so the caller can surface them
    # in the result summary (don't silently swallow failures).
    return {"refreshed": refreshed, "failed": failed}


# Namespace constant for our advisory locks — isolates them from any other
# pg_advisory_lock usage in the DB. Two-int form: (namespace, hashed_key).
ADVISORY_NAMESPACE_INVOICE = 9001


def acquire_invoice_lock(conn, qbo_invoice_id):
    """Try to acquire a session-level advisory lock on this invoice.
    Returns True if acquired, False if another session holds it.
    Caller MUST release via release_invoice_lock (see process_one's finally).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT pg_try_advisory_lock(%s, hashtext(%s))",
        (ADVISORY_NAMESPACE_INVOICE, qbo_invoice_id),
    )
    got = bool(cur.fetchone()[0])
    cur.close()
    return got


def release_invoice_lock(conn, qbo_invoice_id):
    """Releases the advisory lock. Safe to call even if we don't hold it
    (Postgres logs a warning but doesn't raise)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT pg_advisory_unlock(%s, hashtext(%s))",
        (ADVISORY_NAMESPACE_INVOICE, qbo_invoice_id),
    )
    cur.close()


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
                   charge_amount, dry_run):
    """WRITE-AHEAD: insert pending attempt with fresh idempotency_key BEFORE any external call."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO billing.processing_attempts (
            wo_number, invoice_number, qbo_invoice_id, stage, status,
            idempotency_key, payment_method, charge_amount, dry_run
        ) VALUES (%s, %s, %s, 'process', 'pending', %s, %s, %s, %s)
        RETURNING *
    """, (wo_number, invoice_number, qbo_invoice_id, str(uuid.uuid4()),
          payment_method, charge_amount, dry_run))
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
        _dumps(qbo_invoice),
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

    # 1. User override — try to satisfy preferred_type if it's a valid default
    if preferred_type in ("card", "ach"):
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
        """, (customer_id, preferred_type))
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
        "payment_type": row["type"],
        "method_id": row["qbo_payment_method_id"],
        "cpm_id": str(row["id"]),
        "last4": row.get("last_four"),
        "is_default": bool(row.get("is_default")),
        "picked_reason": picked_reason,
    }
    if row["type"] == "card":
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
    try:
        body = resp.json()
        base["raw_response"] = body
    except Exception:
        base["raw_text"] = resp.text[:500]
        return base

    if classification == "success":
        return {**base,
                "charge_id": body.get("id"),
                "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode"),
                "status": body.get("status"),
                "card_last4": (body.get("card") or {}).get("number", "")[-4:],
                "card_type": (body.get("card") or {}).get("cardType"),
                "created": body.get("created")}

    err = body.get("errors", [{}])[0].get("message") if body.get("errors") else None
    return {**base, "error": err or f"status={body.get('status')}"}


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
    try:
        body = resp.json()
        base["raw_response"] = body
    except Exception:
        base["raw_text"] = resp.text[:500]
        return base

    if classification == "success":
        return {**base,
                "charge_id": body.get("id"),
                "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode", ""),
                "status": body.get("status"),
                "card_last4": (body.get("bankAccount") or {}).get("accountNumber", "")[-4:],
                "card_type": "ACH",
                "created": body.get("created")}

    err = body.get("errors", [{}])[0].get("message") if body.get("errors") else None
    return {**base, "error": err or f"status={body.get('status')}"}


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
        return {"success": False, "error": resp.text[:400], "status_code": resp.status_code}

    payment = resp.json().get("Payment", {})
    return {"success": True,
            "payment_id": payment.get("Id"),
            "payment_ref": payment.get("PaymentRefNum"),
            "total_amt": payment.get("TotalAmt")}


def bump_due_date_to_today(invoice_id, access_token, realm_id, max_retries=2):
    """PATCH invoice.DueDate to today so the emailed PDF never reads "past due"
    when we process behind schedule. Sparse update — preserves SyncToken
    and every other field. No-op if DueDate is already today or in the
    future (preserves net terms the office may have set intentionally).

    Retries once on 'Stale Object' (concurrent modification) — re-fetches
    the invoice to get the new SyncToken and retries the PATCH.

    Returns:
      {"updated": bool, "old_due_date": str|None, "new_due_date": str|None,
       "skipped_reason": str|None, "error": str|None, "attempts": int}
    """
    today_str = date.today().strftime("%Y-%m-%d")
    last_err = None

    for attempt in range(max_retries + 1):
        inv_resp = qbo_get(f"invoice/{invoice_id}", access_token, realm_id)
        if not inv_resp.ok:
            last_err = f"fetch failed: {inv_resp.status_code}"
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return {"updated": False, "error": last_err, "attempts": attempt + 1}

        inv = inv_resp.json().get("Invoice", {})
        old = inv.get("DueDate")

        if old and old >= today_str:
            return {"updated": False, "old_due_date": old, "new_due_date": old,
                    "skipped_reason": "already_current", "attempts": attempt + 1}

        sync_token = inv.get("SyncToken")
        if sync_token is None:
            return {"updated": False, "error": "no SyncToken", "attempts": attempt + 1}

        payload = {
            "Id": invoice_id,
            "SyncToken": sync_token,
            "DueDate": today_str,
            "sparse": True,
        }
        resp = qbo_post("invoice", access_token, realm_id, payload)
        if resp.ok:
            return {"updated": True, "old_due_date": old, "new_due_date": today_str,
                    "attempts": attempt + 1}

        text = resp.text[:400]
        last_err = f"HTTP {resp.status_code}: {text}"
        # Stale Object = someone modified the invoice between our fetch and
        # POST. Re-fetch to get the new SyncToken and retry.
        if "Stale Object" in text and attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
            continue
        break

    return {"updated": False, "old_due_date": None, "error": last_err,
            "attempts": max_retries + 1}


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
    payment_method = invoice.get("payment_method")

    if payment_method not in ("on_file", "invoice"):
        return _result(qbo_invoice_id, "error",
                       error=f"invalid payment_method '{payment_method}' (must be on_file or invoice)")

    if invoice.get("billing_status") != "ready_to_process" and not (force or recover_orphan):
        return _result(qbo_invoice_id, "skipped",
                       reason=f"billing_status='{invoice.get('billing_status')}' (need ready_to_process or force=True)")

    # 0. CONCURRENT-RUN GUARD: acquire an advisory lock keyed on this invoice.
    # Prevents two parallel workers from creating separate attempts with
    # different idempotency_keys and firing two charges at Intuit. Lock is
    # session-scoped; the try/finally below guarantees release on every
    # return path (including exceptions).
    if not acquire_invoice_lock(conn, qbo_invoice_id):
        return _result(qbo_invoice_id, "skipped",
                       reason="another worker is already processing this invoice")

    try:
        return _process_one_locked(
            conn, qbo_invoice_id, invoice, wo, wo_number, invoice_number,
            customer_id, customer_name, payment_method,
            access_token, realm_id, dry_run, recover_orphan, force,
        )
    except Exception as e:
        # Flip any still-pending attempt for this invoice to 'error' so it
        # doesn't spin forever in the UI. Bug that stranded MCDONALD +
        # OBRIEN: _process_one_locked threw a serialization exception after
        # creating the pending attempt row; outer main() caught it but never
        # updated the attempt, leaving it in 'pending' → UI showed "Charging
        # card..." indefinitely.
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE billing.processing_attempts
                SET status = 'error',
                    error_message = %s
                WHERE qbo_invoice_id = %s
                  AND stage = 'process'
                  AND status IN ('pending', 'charge_uncertain')
                  AND dry_run = false
                  AND attempted_at > now() - interval '10 minutes'
            """, (f"process_one exception: {str(e)[:200]}", qbo_invoice_id))
            conn.commit()
            cur.close()
        except Exception as inner:
            print(f"  (failed to flip stuck attempt to error: {inner})")
        raise
    finally:
        release_invoice_lock(conn, qbo_invoice_id)


def _process_one_locked(conn, qbo_invoice_id, invoice, wo, wo_number, invoice_number,
                         customer_id, customer_name, payment_method,
                         access_token, realm_id, dry_run, recover_orphan, force):
    """Body of process_one that runs while holding the invoice advisory lock.
    Split out so the lock acquire/release lives in one clean try/finally
    rather than being sprinkled across every return path."""

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

    # Already done — prior attempt succeeded. Trust our own attempt log:
    # the work we were supposed to do (charge OR send invoice email) was
    # done. Mark processed unconditionally. The balance=0 guard previously
    # used here was wrong for invoice-email-path invoices — those have
    # balance > 0 by definition (customer hasn't paid yet), so they'd
    # never get marked processed on re-run. Refresh the QBO cache on the
    # way out as a courtesy but don't gate the state transition on it.
    if prior and prior["status"] == "succeeded":
        qbo_inv, err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if qbo_inv:
            refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv)
        mark_invoice_processed(conn, qbo_invoice_id)
        return _result(qbo_invoice_id, "already_succeeded",
                       attempt_id=str(prior["id"]))

    # Halt for human-required states
    if prior and prior["status"] == "payment_orphan":
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       charge_id=prior["charge_id"],
                       amount=float(prior["charge_amount"] or 0),
                       attempt_id=str(prior["id"]))

    if prior and prior["status"] == "charge_declined" and not force:
        return _result(qbo_invoice_id, "needs_human", reason="charge_declined",
                       error=prior.get("error_message"),
                       attempt_id=str(prior["id"]))

    # email_failed is a halt state — money already moved (for charge path)
    # or email is the deliverable (for invoice-only path). Retrying would
    # re-send emails to whoever the QBO address is (often bogus or empty).
    # Human picks: resolve the email issue and force re-run, or use the
    # "Mark as processed" override to close without sending.
    if prior and prior["status"] == "email_failed" and not force:
        return _result(qbo_invoice_id, "needs_human", reason="email_failed",
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

    # 3. Reuse existing pending/uncertain attempt (preserves idempotency_key) or create new
    if prior and prior["status"] in ("pending", "charge_uncertain"):
        attempt = prior
    else:
        attempt = create_attempt(conn, qbo_invoice_id, wo_number, invoice_number,
                                  payment_method, qbo_balance, dry_run)

    # 4. DRY-RUN short-circuit
    if dry_run:
        plan = _build_dry_run_plan(payment_method, qbo_balance, qbo_email_sent,
                                    customer_id, conn, attempt,
                                    preferred_type=invoice.get("preferred_payment_type"),
                                    credit_review_overridden=bool(invoice.get("credit_review_overridden_at")))
        # Tie the dry-run attempt to the exact payment method row that WOULD
        # have been charged, so the audit trail mirrors live runs.
        pm_on_file = plan.get("payment_method_on_file") or {}
        cpm_id = pm_on_file.get("cpm_id")
        update_attempt(conn, attempt["id"], status="succeeded",
                        raw_result=_dumps(plan),
                        customer_payment_method_id=cpm_id)
        return _result(qbo_invoice_id, "dry_run_complete",
                       attempt_id=str(attempt["id"]),
                       plan=plan)

    # 5. ROUTE
    if payment_method == "invoice":
        return _process_invoice_only(conn, attempt, invoice, qbo_inv, customer_id,
                                      access_token, realm_id)

    # payment_method == 'on_file'
    return _process_charge_path(conn, attempt, invoice, qbo_inv, customer_id, customer_name,
                                 wo_number, invoice_number, qbo_balance, access_token, realm_id)


def _build_dry_run_plan(payment_method, balance, email_already_sent, customer_id,
                         conn, attempt, preferred_type=None,
                         credit_review_overridden=False):
    plan = {
        "payment_method": payment_method,
        "amount_to_charge": balance if payment_method == "on_file" and balance > 0 else 0,
        "would_send_invoice_email": not email_already_sent,
        "would_send_receipt": payment_method == "on_file" and balance > 0,
        "idempotency_key": attempt["idempotency_key"],
    }
    if payment_method == "on_file" and balance > 0:
        # Mirror the live halts in the plan so dry-run accurately predicts
        # what WOULD happen — surfaces credit-check blocks and missing
        # payment methods without actually charging. Respect the office's
        # credit_review override (same logic as live path).
        if credit_review_overridden:
            remaining_credits = []
        else:
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
        # Dry-run plan uses the same preferred_type as the live path so
        # what you see in the plan is what you'd get.
        pm = get_active_payment_method(conn, customer_id,
                                        preferred_type=preferred_type)
        plan["payment_method_on_file"] = pm
        if not pm.get("has_method"):
            plan["would_fail"] = "no_payment_method"
    return plan


def _process_charge_path(conn, attempt, invoice, qbo_inv, customer_id, customer_name,
                          wo_number, invoice_number, balance, access_token, realm_id):
    qbo_invoice_id = invoice["qbo_invoice_id"]

    # If balance is 0 (covered by credits in pre_process), skip charge — just send invoice email + mark done
    if balance == 0:
        # Bump DueDate before the zero-balance invoice email so it doesn't read past due
        due_update = bump_due_date_to_today(qbo_invoice_id, access_token, realm_id)
        print(f"  due_date (zero balance path): {due_update}")
        email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
        update_attempt(conn, attempt["id"], email_sent=email["success"],
                        raw_result=_dumps({"email": email, "skipped_charge_zero_balance": True}))
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
    #
    # RESPECT the office's credit_review override: if the user already
    # reviewed these credits on the detail page and decided they're not
    # applicable to this invoice, skip the recheck. Otherwise the override
    # in pre_process gets ignored at process-time and the invoice keeps
    # flipping back to needs_review no matter how many times they override.
    if invoice.get("credit_review_overridden_at"):
        remaining_credits = []
    else:
        remaining_credits = load_applicable_credits(conn, customer_id)
    if remaining_credits:
        total_unapplied = sum(float(c.get("unapplied_amt") or 0) for c in remaining_credits)
        reason = f"credits_available ({len(remaining_credits)} credit(s), ${total_unapplied:.2f} unapplied)"
        update_attempt(conn, attempt["id"], status="charge_declined",
                        error_message=reason,
                        charge_result=_dumps({"credits_found": remaining_credits}))
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

    # (DueDate bump moved to immediately before email send — only matters
    # for the PDF attached to the email, which is the customer-facing artifact.)

    # Get payment instrument from the DB cache (pull script refreshes every 4h;
    # process_invoice links the exact DB row to the attempt for audit).
    # invoice.preferred_payment_type ('card' or 'ach') is the per-invoice
    # user override set from the detail page — honored when it's a valid
    # default method on file.
    pm = get_active_payment_method(conn, customer_id,
                                    preferred_type=invoice.get("preferred_payment_type"))
    if not pm.get("has_method"):
        update_attempt(conn, attempt["id"], status="charge_declined",
                        error_message=pm.get("error", "no payment method"),
                        charge_result=_dumps(pm))
        return _result(qbo_invoice_id, "needs_human", reason="no_payment_method",
                       attempt_id=str(attempt["id"]),
                       error=pm.get("error"))

    # Pin the attempt to the chosen payment method NOW — before we fire any
    # external calls. Idempotency_key + cpm_id together form the full audit
    # trail even if the charge request fails or the row is later deactivated.
    update_attempt(conn, attempt["id"], customer_payment_method_id=pm["cpm_id"])

    # CHARGE — pass attempt.idempotency_key as Request-Id (this is what makes retry safe)
    if pm["payment_type"] == "card":
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
                        charge_result=_dumps(cr),
                        error_message=cr.get("error"))
        return _result(qbo_invoice_id, "uncertain",
                       attempt_id=str(attempt["id"]),
                       error=cr.get("error"),
                       note="charge state unknown — reconcile_payments will resolve, or retry safely (idempotency_key reused)")

    if classification == "declined":
        update_attempt(conn, attempt["id"], status="charge_declined",
                        charge_result=_dumps(cr),
                        error_message=cr.get("error"))
        return _result(qbo_invoice_id, "needs_human", reason="charge_declined",
                       attempt_id=str(attempt["id"]),
                       error=cr.get("error"))

    # CHARGE SUCCEEDED — persist charge_id IMMEDIATELY before attempting record_payment
    update_attempt(conn, attempt["id"], status="charge_succeeded",
                    charge_id=cr["charge_id"],
                    charge_result=_dumps(cr))

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

    # Bump the invoice DueDate to today so the invoice email PDF doesn't
    # read "past due" when we're processing a backlog. Stale Object retry
    # is built in. Non-fatal — proceed with emails even if bump fails.
    due_update = bump_due_date_to_today(qbo_invoice_id, access_token, realm_id)
    print(f"  due_date: {due_update}")

    # Send BOTH the invoice email (customer sees line items + paid state)
    # AND the payment receipt (confirms the charge). Order matters only for
    # customer inbox appearance; both are independent QBO endpoint calls.
    invoice_email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
    receipt_email = send_payment_receipt(pay["payment_id"], customer_id, access_token, realm_id)

    # Refresh cached balance + EmailStatus
    fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if fresh:
        refresh_invoice_cache(conn, qbo_invoice_id, fresh)

    # email_sent tracks success of BOTH emails — if either failed, the
    # office needs to investigate (usually "customer has no email on file").
    # Attempt status=email_failed leaves the invoice in ready_to_process
    # until a human either resolves the email issue or explicitly pushes
    # to processed via the manual override. Money moved; no re-charge risk
    # because the state machine treats email_failed as a halt state.
    both_sent = invoice_email["success"] and receipt_email["success"]
    failures = []
    if not invoice_email["success"]:
        failures.append(f"invoice_email: {invoice_email.get('error', 'unknown')[:120]}")
    if not receipt_email["success"]:
        failures.append(f"receipt_email: {receipt_email.get('error', 'unknown')[:120]}")

    update_attempt(conn, attempt["id"], email_sent=both_sent,
                    status="succeeded" if both_sent else "email_failed",
                    error_message=None if both_sent else "; ".join(failures),
                    raw_result=_dumps({"payment": pay,
                                            "invoice_email": invoice_email,
                                            "receipt_email": receipt_email,
                                            "due_date": due_update}))
    if both_sent:
        mark_invoice_processed(conn, qbo_invoice_id)
    # If email_failed: invoice STAYS in ready_to_process. Human uses the
    # "Mark as processed" override on the detail page to close it out.
    return _result(qbo_invoice_id, "succeeded" if both_sent else "email_failed",
                   attempt_id=str(attempt["id"]),
                   charge_id=cr["charge_id"],
                   qbo_payment_id=pay["payment_id"],
                   invoice_email_sent=invoice_email["success"],
                   receipt_email_sent=receipt_email["success"],
                   email_errors=failures or None)


def _retry_record_payment_for_orphan(conn, prior, invoice, customer_id, customer_name,
                                      wo_number, invoice_number, access_token, realm_id):
    """Resume from charge_succeeded or payment_orphan: try record_payment with persisted charge_id.
    Does NOT charge again. Idempotency_key is reused via the charge_id (already in QBO Intuit Payments).

    IMPORTANT: if prior.qbo_payment_id is already set, the Payment record
    was created on a previous attempt (worker crashed between record_payment
    success and the final status='succeeded' write). Calling record_qbo_payment
    again would create a DUPLICATE QBO Payment. Skip straight to email + mark
    processed.
    """
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
    prior_payment_id = prior.get("qbo_payment_id")

    if prior_payment_id:
        # Ledger write already succeeded on a prior attempt — don't double-record.
        # Fast-forward to email + mark processed.
        pay = {"success": True, "payment_id": prior_payment_id,
                "reused_from_prior_attempt": True}
    else:
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

    # Bump DueDate immediately before sending emails.
    due_update = bump_due_date_to_today(qbo_invoice_id, access_token, realm_id)
    print(f"  due_date: {due_update}")

    invoice_email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
    receipt_email = send_payment_receipt(pay["payment_id"], customer_id, access_token, realm_id)

    fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if fresh:
        refresh_invoice_cache(conn, qbo_invoice_id, fresh)

    both_sent = invoice_email["success"] and receipt_email["success"]
    failures = []
    if not invoice_email["success"]:
        failures.append(f"invoice_email: {invoice_email.get('error', 'unknown')[:120]}")
    if not receipt_email["success"]:
        failures.append(f"receipt_email: {receipt_email.get('error', 'unknown')[:120]}")

    update_attempt(conn, prior["id"], email_sent=both_sent,
                    status="succeeded" if both_sent else "email_failed",
                    error_message=None if both_sent else "; ".join(failures),
                    raw_result=_dumps({"orphan_recovery": True, "payment": pay,
                                            "invoice_email": invoice_email,
                                            "receipt_email": receipt_email,
                                            "due_date": due_update}))
    if both_sent:
        mark_invoice_processed(conn, qbo_invoice_id)
    return _result(qbo_invoice_id, "succeeded" if both_sent else "email_failed",
                   attempt_id=str(prior["id"]),
                   charge_id=charge_id,
                   qbo_payment_id=pay["payment_id"],
                   recovered_from="orphan_or_charge_succeeded",
                   skipped_record_payment=bool(prior_payment_id),
                   invoice_email_sent=invoice_email["success"],
                   receipt_email_sent=receipt_email["success"],
                   email_errors=failures or None)


def _process_invoice_only(conn, attempt, invoice, qbo_inv, customer_id, access_token, realm_id):
    """payment_method='invoice' — email IS the deliverable. Auto-retry email up to N times."""
    qbo_invoice_id = invoice["qbo_invoice_id"]

    # Bump DueDate to today before emailing so the invoice PDF never reads
    # "past due" when we're processing a backlog. bump_due_date_to_today
    # is a no-op if the existing DueDate is already today-or-later.
    due_update = bump_due_date_to_today(qbo_invoice_id, access_token, realm_id)
    print(f"  due_date: {due_update}")

    last_err = None
    for i in range(EMAIL_RETRY_MAX):
        email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
        if email["success"]:
            update_attempt(conn, attempt["id"], status="succeeded", email_sent=True,
                            raw_result=_dumps({"email": email, "attempts": i + 1}))
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
                    raw_result=_dumps({"attempts": EMAIL_RETRY_MAX, "last_error": last_err}))
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

        # Determine target list FIRST so we can scope the preflight refresh
        # to exactly the customers we're about to touch.
        if qbo_invoice_id and not qbo_invoice_ids:
            targets = [qbo_invoice_id]
        elif qbo_invoice_ids:
            targets = list(qbo_invoice_ids)
        elif bulk_all:
            cur = conn.cursor()
            sql = ("SELECT qbo_invoice_id FROM billing.invoices "
                   "WHERE billing_status = 'ready_to_process' "
                   "ORDER BY txn_date DESC NULLS LAST")
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            targets = [r[0] for r in cur.fetchall()]
            cur.close()
        else:
            targets = []

        # Preflight: if methods / credits caches are stale, refresh them ONCE
        # before any invoice gets processed. Scoped to the target customers
        # so the refresh is guaranteed to touch everyone we'll charge —
        # bypasses the pull script's internal 4h TTL. Skip for dry-run and
        # recover_orphan (narrow-scope retry of a known-charged invoice).
        preflight_result = {"refreshed": [], "failed": []}
        target_customer_ids = []
        if targets and not dry_run and not recover_orphan:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT qbo_customer_id FROM billing.invoices "
                "WHERE qbo_invoice_id = ANY(%s) AND qbo_customer_id IS NOT NULL",
                (targets,),
            )
            target_customer_ids = [r[0] for r in cur.fetchall()]
            cur.close()
            preflight_result = preflight_refresh_if_stale(
                conn, target_customer_ids=target_customer_ids,
            )

        # Single mode
        if qbo_invoice_id and not qbo_invoice_ids:
            single = process_one(conn, qbo_invoice_id, access_token, realm_id,
                                  dry_run=dry_run, recover_orphan=recover_orphan, force=force)
            single["preflight"] = preflight_result
            return single

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
                "dry_run": dry_run,
                "preflight": preflight_result}

    finally:
        conn.close()
