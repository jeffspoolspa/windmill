# f/service_billing/push_invoice_edits
#
# Apply user edits from the classification editor to QBO + cache atomically.
# Pushes memo + statement_memo + qbo_class to QBO via PATCH, then writes the
# same values to billing.invoices along with memo_locked=true + enrichment_ok=true.
#
# Different from pre_process_invoice:
#   - Does NOT regenerate the memo via OpenAI (user-supplied)
#   - Does NOT re-derive qbo_class (user-supplied)
#   - Sets memo_locked=true (user has affirmed the memo)
#   - Clears needs_review_reason entirely (user is asserting the issues are
#     resolved); recheck_invoice_status rebuilds the deterministic ones
#     (subtotal_mismatch, credit_review) if still applicable
#   - Sets enrichment_ok=true (the QBO write went through)
#   - When payment_method changes, ALSO updates preferred_payment_type AND
#     target_payment_method_id so the new model stays in sync. 'invoice' →
#     'email' + NULL target. 'on_file' → most-recent-default PM's type + uuid.
#
# After this script returns, billing.recheck_invoice_status (called inline)
# recomputes billing_status based on the now-cleaner needs_review_reason
# and whatever subtotal/credit reasons may remain.
#
# Idempotent — safe to retry. The QBO PATCH is sparse, won't double-apply.

import json
import time
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    return obj


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


def fetch_qbo_classes(access_token, realm_id):
    resp = qbo_get("query", access_token, realm_id,
                   params={"query": "SELECT * FROM Class WHERE Active = true MAXRESULTS 1000"})
    if not resp.ok:
        return {}
    classes = resp.json().get("QueryResponse", {}).get("Class", [])
    return {c["Name"].lower(): c["Id"] for c in classes}


def update_qbo_with_retry(qbo_invoice_id, updates, access_token, realm_id, max_retries=2):
    last_err = None
    for attempt in range(max_retries + 1):
        inv_resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)
        if not inv_resp.ok:
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return {"success": False, "error": f"fetch failed: {inv_resp.status_code}"}
        inv = inv_resp.json().get("Invoice")
        if not inv:
            return {"success": False, "error": "QBO returned no Invoice"}
        body = {"Id": inv["Id"], "SyncToken": inv["SyncToken"], "sparse": True, **updates}
        resp = qbo_post("invoice", access_token, realm_id, body)
        if resp.ok:
            return {"success": True, "invoice": resp.json().get("Invoice")}
        text = resp.text[:400]
        last_err = f"HTTP {resp.status_code}: {text}"
        if "Stale Object" in text and attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    return {"success": False, "error": last_err}


def derive_new_payment_fields(conn, qbo_invoice_id, payment_method):
    """Translate the legacy payment_method dropdown value into the new
    preferred_payment_type + target_payment_method_id pair.

      'invoice' (email)  → preferred_payment_type='email',  target=NULL
      'on_file' (charge) → preferred_payment_type=<type>,   target=<uuid>
                          where <type, uuid> is the customer's most-recently-
                          added default PM. Falls back to ('email', NULL) if
                          no default is on file (matches resolve_preferred_
                          payment_type's behavior).

    Returns (preferred_payment_type, target_payment_method_id). Returns
    (None, None) if payment_method is None or unknown — caller should leave
    those fields untouched.
    """
    if payment_method == "invoice":
        return ("email", None)

    if payment_method != "on_file":
        return (None, None)  # unknown — don't touch

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT cpm.id, cpm.type
          FROM billing.customer_payment_methods cpm
          JOIN billing.invoices i ON i.qbo_customer_id = cpm.qbo_customer_id
         WHERE i.qbo_invoice_id = %s
           AND cpm.is_active = true
           AND cpm.is_default = true
         ORDER BY (cpm.raw->>'created') DESC NULLS LAST,
                  cpm.fetched_at DESC
         LIMIT 1
    """, (qbo_invoice_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        # No default PM available — process_invoice would fail anyway,
        # so resolve to email to keep the user from clicking Process and
        # hitting a no_payment_method error.
        return ("email", None)
    return (row["type"], str(row["id"]))


def main(qbo_invoice_id: str,
         qbo_class: str = None,
         payment_method: str = None,
         memo: str = None,
         statement_memo: str = None):
    """
    Returns:
      {"status": "ok", "billing_status": "...", "invoice": {...}}
      {"status": "error", "error": "..."}
    """
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}

    print(f"=== push_invoice_edits {qbo_invoice_id} ===")
    access_token, realm_id = refresh_qbo_token()

    # Build the QBO PATCH body. Only include fields the user provided.
    updates = {}
    composed_memo = memo
    composed_statement = statement_memo or memo

    if composed_memo is not None:
        updates["PrivateNote"] = composed_memo
    if composed_statement is not None:
        updates["CustomerMemo"] = {"value": composed_statement}

    if qbo_class:
        classes = fetch_qbo_classes(access_token, realm_id)
        class_id = classes.get(qbo_class.lower())
        if not class_id:
            return {"status": "error",
                    "error": f"unknown qbo_class '{qbo_class}' (valid: {sorted(classes.keys())})"}
        updates["ClassRef"] = {"value": class_id, "name": qbo_class}

    if not updates:
        # Nothing to push to QBO — just update cache state for memo_locked + enrichment
        print("  no QBO fields to push, updating cache flags only")
    else:
        # Push to QBO
        result = update_qbo_with_retry(qbo_invoice_id, updates, access_token, realm_id)
        if not result["success"]:
            return {"status": "error",
                    "error": f"QBO PATCH failed: {result.get('error')}",
                    "qbo_invoice_id": qbo_invoice_id}
        print(f"  QBO PATCH ok ({list(updates.keys())})")

        # Track that we expect a corresponding QBO webhook back. The webhook
        # handler's confirm_webhook_expectation RPC will mark this row
        # confirmed when the matching invoice.update event arrives. If it
        # doesn't arrive within the grace window, the cdc_reconciler flips
        # the row to status='missing' — surfaces in the sync issues badge.
        try:
            _exp_conn = get_db_conn()
            _cur = _exp_conn.cursor()
            _cur.execute("""
                INSERT INTO billing.webhook_expectations
                  (entity_type, entity_id, expected_by, source)
                VALUES ('Invoice', %s, now() + interval '5 minutes', 'self_initiated')
            """, (qbo_invoice_id,))
            _exp_conn.commit()
            _cur.close()
            _exp_conn.close()
        except Exception as e:
            # Expectation tracking is observability — don't fail the operation
            # if we can't write the row. Log and continue.
            print(f"  (webhook_expectation insert failed: {e})")

    # Update the cache. Set memo_locked=true and enrichment_ok=true. Clear
    # needs_review_reason entirely — the user is asserting the invoice is
    # ready, so any non-deterministic reasons (memo_low_confidence,
    # user_revert, qbo_write_failed, memo_api_error) should be wiped.
    # recheck_invoice_status rebuilds the deterministic reasons
    # (subtotal_mismatch, credit_review) below if any still apply.
    #
    # If payment_method changed, ALSO derive + write the new vocab fields
    # so process_invoice charges the right thing.
    conn = get_db_conn()
    try:
        # Derive new payment fields from the legacy dropdown value.
        new_preferred, new_target = (None, None)
        explicitly_set_pm_fields = False
        if payment_method is not None:
            new_preferred, new_target = derive_new_payment_fields(
                conn, qbo_invoice_id, payment_method
            )
            explicitly_set_pm_fields = True
            print(f"  payment_method={payment_method!r} → preferred={new_preferred!r}, target={new_target}")

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Two UPDATE shapes depending on whether we're changing PM fields.
        # Avoids overwriting target_payment_method_id with NULL when the
        # user didn't change payment_method.
        if explicitly_set_pm_fields:
            cur.execute("""
                UPDATE billing.invoices
                SET memo                      = COALESCE(%s, memo),
                    statement_memo            = COALESCE(%s, statement_memo),
                    qbo_class                 = COALESCE(%s, qbo_class),
                    payment_method            = %s,
                    preferred_payment_type    = %s,
                    target_payment_method_id  = %s,
                    memo_locked               = true,
                    enrichment_ok             = true,
                    needs_review_reason       = NULL,
                    fetched_at                = now()
                WHERE qbo_invoice_id = %s
                RETURNING qbo_invoice_id, billing_status, needs_review_reason,
                          memo, statement_memo, qbo_class, payment_method,
                          preferred_payment_type, target_payment_method_id,
                          memo_locked, enrichment_ok
            """, (composed_memo, composed_statement, qbo_class,
                  payment_method, new_preferred, new_target,
                  qbo_invoice_id))
        else:
            cur.execute("""
                UPDATE billing.invoices
                SET memo                = COALESCE(%s, memo),
                    statement_memo      = COALESCE(%s, statement_memo),
                    qbo_class           = COALESCE(%s, qbo_class),
                    memo_locked         = true,
                    enrichment_ok       = true,
                    needs_review_reason = NULL,
                    fetched_at          = now()
                WHERE qbo_invoice_id = %s
                RETURNING qbo_invoice_id, billing_status, needs_review_reason,
                          memo, statement_memo, qbo_class, payment_method,
                          preferred_payment_type, target_payment_method_id,
                          memo_locked, enrichment_ok
            """, (composed_memo, composed_statement, qbo_class, qbo_invoice_id))

        updated = cur.fetchone()
        if not updated:
            conn.rollback()
            return {"status": "error",
                    "error": f"invoice {qbo_invoice_id} not in billing.invoices"}

        # Run recheck to recompute billing_status from current state. Will
        # rebuild needs_review_reason with subtotal_mismatch / credit_review
        # if they still apply against the now-clean slate.
        cur.execute("SELECT billing.recheck_invoice_status(%s) AS r", (qbo_invoice_id,))
        recheck = cur.fetchone()["r"]
        conn.commit()
        cur.close()

        return {
            "status": "ok",
            "qbo_invoice_id": qbo_invoice_id,
            "billing_status": recheck.get("new_billing_status"),
            "needs_review_reason": recheck.get("new_reason"),
            "invoice": _json_safe(dict(updated)),
            "qbo_pushed": list(updates.keys()),
            "recheck": _json_safe({
                "changed": recheck.get("changed"),
                "prev_billing_status": recheck.get("prev_billing_status"),
                "new_billing_status": recheck.get("new_billing_status"),
            }),
        }
    finally:
        conn.close()
