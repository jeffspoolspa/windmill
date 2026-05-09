# f/service_billing/refresh_payment
#
# Single-payment QBO -> Supabase refresh.
#
# Callers:
#   - QBO webhook handler:   main(qbo_payment_id)
#                            — fetches the payment from QBO and refreshes
#   - cdc_reconciler:        main(qbo_payment_id, qbo_body=<cdc_entity>)
#                            — passes the body it already has from CDC,
#                              skipping the QBO GET. Single source of truth
#                              for the upsert + side effects.
#
# Concurrency: the upsert uses an OCC guard on qbo_last_updated_time.
# Two concurrent callers writing the same payment never clobber each other —
# whichever has the newer QBO timestamp wins, the other's UPDATE is a no-op.
#
# Side effects (CCTransId verification, linked-invoice rechecks) run even
# when did_write is false — they read current state, not "what we just wrote",
# so they're safe and useful regardless.

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
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
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


def upsert_payment(conn, qbo_pmt):
    """Upsert with OCC guard. Returns (qbo_payment_id, did_write, payment_row).

    OCC: only updates when EXCLUDED.qbo_last_updated_time is strictly newer
    than the existing row's. New inserts (no conflict) always land.
    Race-loser's UPDATE matches zero rows; no harm because their data was
    older anyway.
    """
    customer_ref = qbo_pmt.get("CustomerRef") or {}
    payment_method_ref = qbo_pmt.get("PaymentMethodRef") or {}
    cc_info = qbo_pmt.get("CreditCardPayment") or {}
    cc_response = cc_info.get("CreditChargeResponse") or {}

    qbo_payment_id      = qbo_pmt.get("Id")
    qbo_customer_id     = customer_ref.get("value")
    total_amt           = float(qbo_pmt.get("TotalAmt") or 0)
    unapplied_amt       = float(qbo_pmt.get("UnappliedAmt") or 0)
    txn_date            = qbo_pmt.get("TxnDate")
    ref_num             = qbo_pmt.get("PaymentRefNum")
    memo                = qbo_pmt.get("PrivateNote")
    payment_method_id   = payment_method_ref.get("value")
    payment_method_name = payment_method_ref.get("name")
    qbo_last_updated    = parse_qbo_timestamp(
        (qbo_pmt.get("MetaData") or {}).get("LastUpdatedTime")
    )

    # cc_trans_id and cc_status are GENERATED ALWAYS columns derived from
    # raw->'CreditCardPayment'->'CreditChargeResponse'. We write raw and
    # they auto-populate; writing them directly raises a Postgres error.
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO billing.customer_payments
          (qbo_payment_id, qbo_customer_id, type, total_amt, unapplied_amt,
           txn_date, ref_num, memo, payment_method_id, payment_method_name,
           raw, fetched_at,
           qbo_last_updated_time, sync_state, sync_state_changed_at)
        VALUES (%s, %s, 'payment', %s, %s, %s, %s, %s, %s, %s,
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
          raw                   = EXCLUDED.raw,
          fetched_at            = now(),
          qbo_last_updated_time = EXCLUDED.qbo_last_updated_time,
          sync_state            = 'synced',
          sync_state_changed_at = now(),
          sync_error            = NULL
        WHERE billing.customer_payments.qbo_last_updated_time IS NULL
           OR EXCLUDED.qbo_last_updated_time IS NULL
           OR billing.customer_payments.qbo_last_updated_time < EXCLUDED.qbo_last_updated_time
        RETURNING *
    """, (
        qbo_payment_id, qbo_customer_id, total_amt, unapplied_amt,
        txn_date, ref_num, memo, payment_method_id, payment_method_name,
        _dumps(qbo_pmt), qbo_last_updated,
    ))
    row = cur.fetchone()
    cur.close()
    return qbo_payment_id, (row is not None), (dict(row) if row else None)


def verify_cc_trans_id(conn, qbo_payment_id, cc_trans_id_from_qbo):
    """Cross-check QBO's CCTransId against our processing_attempts.charge_id.

    Outcomes:
      no_attempt           — payment wasn't created by us (customer-initiated, etc.)
      verified             — match; happy path
      cc_trans_id_missing  — we expected one (we have charge_id) but QBO has none
      cc_trans_id_mismatch — money linked to wrong charge; flag for review
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, charge_id, status, attempted_at
          FROM billing.processing_attempts
         WHERE qbo_payment_id = %s
           AND dry_run = false
         ORDER BY attempted_at DESC
         LIMIT 1
    """, (qbo_payment_id,))
    attempt = cur.fetchone()

    if not attempt:
        cur.close()
        return {"outcome": "no_attempt", "expected": None,
                "actual": cc_trans_id_from_qbo, "attempt_id": None}

    attempt_dict = dict(attempt)
    expected = attempt_dict.get("charge_id")
    attempt_id = str(attempt_dict["id"])

    if not expected:
        cur.close()
        return {"outcome": "no_attempt", "expected": None,
                "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}

    if not cc_trans_id_from_qbo:
        msg = (f"verify_cc_trans_id: missing on QBO Payment {qbo_payment_id}; "
               f"expected charge_id={expected}.")
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


def main(qbo_payment_id: str, qbo_body: dict | None = None):
    """
    Args:
      qbo_payment_id: Required. QBO Id of the payment.
      qbo_body:       Optional. Pre-fetched QBO Payment body (e.g. from CDC).
                      When provided, skips the QBO GET.
    """
    if not qbo_payment_id:
        return {"status": "error", "error": "qbo_payment_id required"}

    print(f"=== refresh_payment {qbo_payment_id} (body_provided={qbo_body is not None}) ===")

    qbo_pmt = qbo_body
    if qbo_pmt is None:
        access_token, realm_id = refresh_qbo_token()
        resp = qbo_get(f"payment/{qbo_payment_id}", access_token, realm_id)

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
            return {"status": "error",
                    "error": f"QBO fetch failed: {resp.status_code}",
                    "detail": resp.text[:200]}

        qbo_pmt = (resp.json() or {}).get("Payment")
        if not qbo_pmt:
            return {"status": "error", "error": "QBO returned no Payment"}

    cc_info = qbo_pmt.get("CreditCardPayment") or {}
    cc_trans_id = (cc_info.get("CreditChargeResponse") or {}).get("CCTransId")

    # Linked invoices need rechecks since this payment may have applied to them.
    linked_invoice_ids = []
    for line in qbo_pmt.get("Line") or []:
        for linked_txn in line.get("LinkedTxn") or []:
            if linked_txn.get("TxnType") == "Invoice":
                inv_id = linked_txn.get("TxnId")
                if inv_id and inv_id not in linked_invoice_ids:
                    linked_invoice_ids.append(inv_id)

    conn = get_db_conn()
    try:
        qbo_payment_id, did_write, upserted = upsert_payment(conn, qbo_pmt)
        conn.commit()

        # CC verify reads QBO body + our processing_attempts; safe regardless.
        verification = verify_cc_trans_id(conn, qbo_payment_id, cc_trans_id)

        # NO MANUAL RECHECK NEEDED.
        # The upsert_payment write to billing.customer_payments fires the
        # fn_set_credits_ok_from_payment trigger automatically, which fans
        # out to every linked invoice for the affected customer, recomputes
        # credits_ok, and (via the projection trigger) updates billing_status
        # in-place. This used to be a manual loop calling recheck_invoice_status
        # — now the database handles it inside the same transaction as the
        # cache write. See migrations 20260508000003..7.
        recheck_results: list[dict] = []  # kept for return-shape compat

        conn.commit()

        if not did_write:
            print(f"  upsert no-op (OCC blocked — newer state already in cache)")

        return {
            "status":                    "ok",
            "qbo_payment_id":            qbo_payment_id,
            "qbo_customer_id":           (qbo_pmt.get("CustomerRef") or {}).get("value"),
            "total_amt":                 float(qbo_pmt.get("TotalAmt") or 0),
            "unapplied_amt":             float(qbo_pmt.get("UnappliedAmt") or 0),
            "did_write":                 did_write,
            "linked_invoices_rechecked": recheck_results,
            "verification":              verification,
            "payment_id":                str(upserted["id"]) if upserted else None,
        }
    finally:
        conn.close()
