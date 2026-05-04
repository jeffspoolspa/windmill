# f/service_billing/refresh_payment
#
# Single-payment QBO → Supabase refresh. Triggered by the QBO webhook handler
# when a Payment is created / updated / deleted in QBO. Fetches the current
# state from QBO, upserts into billing.customer_payments, and rechecks any
# invoices the payment is linked to (since balance/billing_status may have
# flipped as a result).
#
# Mirrors the shape of refresh_invoice:
#   1. Fetch QBO payment
#   2. Upsert into billing.customer_payments (volatile fields only)
#   3. Verify CCTransId on QBO Payment matches our processing_attempts.charge_id
#      (catches the rare case where QBO commits the Payment but drops our
#      CCTransId, which would break Intuit↔QBO reconciliation later)
#   4. Recheck linked invoices' billing_status via billing.recheck_invoice_status
#   5. Return the updated row + recheck summary + any verification warnings
#
# Idempotent: re-runs upsert with same data are no-ops at the DB level.
# Designed to be called repeatedly via webhook re-deliveries.

import json
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _dumps(obj):
    return json.dumps(obj, default=_json_default)


def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
        timeout=30,
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
        host=sb["host"],
        port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"),
        user=sb["user"],
        password=sb["password"],
        sslmode=sb.get("sslmode", "require"),
    )


def qbo_get(path, access_token, realm_id):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )


def parse_qbo_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def verify_cc_trans_id(conn, qbo_payment_id, cc_trans_id_from_qbo):
    """Cross-check the CCTransId on the QBO Payment against our recorded
    charge_id from the original processing_attempts row.

    Three outcomes:
      - 'no_attempt': no processing_attempts row links to this payment.
        It's a customer-initiated payment (or a credit-memo application,
        or refresh fired before our process_invoice script committed
        the qbo_payment_id back). No verification possible — return None.
      - 'verified': attempt.charge_id matches QBO's CCTransId. Happy path.
      - 'cc_trans_id_missing': we expected a CCTransId (we have a charge_id
        on the attempt) but QBO doesn't have one on the Payment. Means our
        record_payment call didn't preserve it — Intuit↔QBO reconciliation
        will be broken. Log warning + stamp the attempt's error_message.
      - 'cc_trans_id_mismatch': QBO has a CCTransId but it's not our
        charge_id. Very serious — money is linked to wrong charge.
        Flag attempt as needs_reconcile_review.

    Returns: {"outcome": str, "expected": str|None, "actual": str|None,
              "attempt_id": str|None}
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Find our own processing_attempts row for this Payment. There can only
    # be one with qbo_payment_id set to this value (we set it once after
    # record_payment succeeds; it's never re-used).
    cur.execute("""
        SELECT id, charge_id, status, attempted_at
          FROM billing.processing_attempts
         WHERE qbo_payment_id = %s
           AND dry_run = false
         ORDER BY attempted_at DESC
         LIMIT 1
    """, (qbo_payment_id,))
    attempt = cur.fetchone()

    # No matching attempt — this Payment wasn't created by us. Skip silently.
    if not attempt:
        cur.close()
        return {"outcome": "no_attempt", "expected": None,
                "actual": cc_trans_id_from_qbo, "attempt_id": None}

    attempt_dict = dict(attempt)
    expected = attempt_dict.get("charge_id")
    attempt_id = str(attempt_dict["id"])

    # Our attempt has no charge_id either (e.g. an email-only attempt that
    # somehow ended up linked to a Payment row, or pre-charge_succeeded
    # state). Nothing to verify.
    if not expected:
        cur.close()
        return {"outcome": "no_attempt", "expected": None,
                "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}

    # CCTransId missing on QBO Payment but we expected one
    if not cc_trans_id_from_qbo:
        msg = (f"verify_cc_trans_id: missing on QBO Payment {qbo_payment_id}; "
               f"expected charge_id={expected}. Intuit↔QBO reconciliation broken.")
        print(f"  WARN  {msg}")
        cur.execute("""
            UPDATE billing.processing_attempts
               SET error_message = COALESCE(error_message, '')
                                   || CASE WHEN COALESCE(error_message, '') = '' THEN '' ELSE ' | ' END
                                   || %s
             WHERE id = %s
        """, (f"cc_trans_id_missing (expected {expected})", attempt_dict["id"]))
        conn.commit()
        cur.close()
        return {"outcome": "cc_trans_id_missing", "expected": expected,
                "actual": None, "attempt_id": attempt_id}

    # CCTransId mismatch — money linked to a different charge than ours
    if cc_trans_id_from_qbo != expected:
        msg = (f"verify_cc_trans_id: MISMATCH on Payment {qbo_payment_id}: "
               f"expected charge_id={expected}, got CCTransId={cc_trans_id_from_qbo}")
        print(f"  ERROR {msg}")
        cur.execute("""
            UPDATE billing.processing_attempts
               SET status = 'needs_reconcile_review',
                   error_message = COALESCE(error_message, '')
                                   || CASE WHEN COALESCE(error_message, '') = '' THEN '' ELSE ' | ' END
                                   || %s
             WHERE id = %s
        """, (f"cc_trans_id_mismatch (expected {expected}, got {cc_trans_id_from_qbo})",
              attempt_dict["id"]))
        conn.commit()
        cur.close()
        return {"outcome": "cc_trans_id_mismatch", "expected": expected,
                "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}

    cur.close()
    return {"outcome": "verified", "expected": expected,
            "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}


def main(qbo_payment_id: str):
    """
    Returns:
      {"status": "ok", "payment": {...}, "linked_invoices_rechecked": [...],
       "verification": {"outcome": "...", ...}}
      {"status": "deleted", ...} if QBO returns 404 (payment voided/deleted)
      {"status": "error", "error": "..."}
    """
    if not qbo_payment_id:
        return {"status": "error", "error": "qbo_payment_id required"}

    print(f"=== refresh_payment {qbo_payment_id} ===")
    access_token, realm_id = refresh_qbo_token()

    # Fetch from QBO
    resp = qbo_get(f"payment/{qbo_payment_id}", access_token, realm_id)

    # Payment was voided/deleted in QBO. Mark it as such locally so the
    # cache reflects the absence; don't attempt to upsert.
    if resp.status_code == 404:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE billing.customer_payments
                SET sync_state = 'synced',
                    sync_state_changed_at = now(),
                    sync_error = 'deleted in QBO',
                    fetched_at = now()
                WHERE qbo_payment_id = %s
            """, (qbo_payment_id,))
            conn.commit()
            cur.close()
        finally:
            conn.close()
        return {"status": "deleted", "qbo_payment_id": qbo_payment_id}

    if not resp.ok:
        return {
            "status": "error",
            "error": f"QBO fetch failed: {resp.status_code}",
            "detail": resp.text[:200],
        }

    qbo_pmt = (resp.json() or {}).get("Payment")
    if not qbo_pmt:
        return {"status": "error", "error": "QBO returned no Payment"}

    # Extract volatile fields
    customer_ref = qbo_pmt.get("CustomerRef") or {}
    qbo_customer_id = customer_ref.get("value")
    total_amt = float(qbo_pmt.get("TotalAmt") or 0)
    unapplied_amt = float(qbo_pmt.get("UnappliedAmt") or 0)
    txn_date = qbo_pmt.get("TxnDate")
    ref_num = qbo_pmt.get("PaymentRefNum")
    memo = qbo_pmt.get("PrivateNote")
    payment_method_ref = qbo_pmt.get("PaymentMethodRef") or {}
    payment_method_id = payment_method_ref.get("value")
    payment_method_name = payment_method_ref.get("name")

    qbo_last_updated = parse_qbo_timestamp(
        (qbo_pmt.get("MetaData") or {}).get("LastUpdatedTime")
    )

    # Pull credit-card info if present (for charged payments)
    cc_info = qbo_pmt.get("CreditCardPayment") or {}
    cc_trans_id = cc_info.get("CreditChargeResponse", {}).get("CCTransId")
    cc_status = cc_info.get("CreditChargeResponse", {}).get("Status")

    # Linked invoices — need to recheck their status since this payment
    # may have applied to one of them.
    linked_invoice_ids = []
    for line in qbo_pmt.get("Line") or []:
        for linked_txn in line.get("LinkedTxn") or []:
            if linked_txn.get("TxnType") == "Invoice":
                inv_id = linked_txn.get("TxnId")
                if inv_id and inv_id not in linked_invoice_ids:
                    linked_invoice_ids.append(inv_id)

    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Upsert. The puller (pull_qbo_credits) inserts the row originally;
        # we update volatile fields here. If the row doesn't exist (rare —
        # webhook arrived before initial pull), we INSERT it as a fresh row.
        cur.execute("""
            INSERT INTO billing.customer_payments
              (qbo_payment_id, qbo_customer_id, type, total_amt, unapplied_amt,
               txn_date, ref_num, memo, payment_method_id, payment_method_name,
               cc_trans_id, cc_status, raw, fetched_at,
               qbo_last_updated_time, sync_state, sync_state_changed_at)
            VALUES (%s, %s, 'payment', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, now(), %s, 'synced', now())
            ON CONFLICT (qbo_payment_id) DO UPDATE SET
              qbo_customer_id       = EXCLUDED.qbo_customer_id,
              total_amt             = EXCLUDED.total_amt,
              unapplied_amt         = EXCLUDED.unapplied_amt,
              txn_date              = EXCLUDED.txn_date,
              ref_num               = EXCLUDED.ref_num,
              memo                  = EXCLUDED.memo,
              payment_method_id     = EXCLUDED.payment_method_id,
              payment_method_name   = EXCLUDED.payment_method_name,
              cc_trans_id           = COALESCE(EXCLUDED.cc_trans_id, billing.customer_payments.cc_trans_id),
              cc_status             = COALESCE(EXCLUDED.cc_status, billing.customer_payments.cc_status),
              raw                   = EXCLUDED.raw,
              fetched_at            = now(),
              qbo_last_updated_time = EXCLUDED.qbo_last_updated_time,
              sync_state            = 'synced',
              sync_state_changed_at = now(),
              sync_error            = NULL
            RETURNING *
        """, (
            qbo_payment_id, qbo_customer_id, total_amt, unapplied_amt,
            txn_date, ref_num, memo, payment_method_id, payment_method_name,
            cc_trans_id, cc_status, _dumps(qbo_pmt), qbo_last_updated,
        ))
        upserted = cur.fetchone()
        cur.close()

        # Verify CCTransId matches our recorded charge_id (if this Payment
        # was one we created via process_invoice). Catches the rare case
        # where QBO commits the Payment but drops our CCTransId — which
        # would silently break Intuit↔QBO reconciliation later.
        verification = verify_cc_trans_id(conn, qbo_payment_id, cc_trans_id)

        # Recheck linked invoices — their billing_status may have flipped
        # to 'processed' if this payment zeroed them out.
        recheck_results = []
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for inv_id in linked_invoice_ids:
            try:
                cur.execute(
                    "SELECT billing.recheck_invoice_status(%s) AS r",
                    (inv_id,),
                )
                r = cur.fetchone()["r"]
                recheck_results.append({
                    "qbo_invoice_id": inv_id,
                    "changed": r.get("changed"),
                    "prev_billing_status": r.get("prev_billing_status"),
                    "new_billing_status": r.get("new_billing_status"),
                })
            except Exception as e:
                recheck_results.append({
                    "qbo_invoice_id": inv_id,
                    "error": str(e)[:200],
                })

        conn.commit()
        cur.close()

        return {
            "status": "ok",
            "qbo_payment_id": qbo_payment_id,
            "qbo_customer_id": qbo_customer_id,
            "total_amt": total_amt,
            "unapplied_amt": unapplied_amt,
            "linked_invoices_rechecked": recheck_results,
            "verification": verification,
            "payment_id": str(upserted["id"]) if upserted else None,
        }
    finally:
        conn.close()
