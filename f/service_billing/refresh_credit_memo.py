# f/service_billing/refresh_credit_memo
#
# Single-CreditMemo QBO -> Supabase refresh. Triggered by QBO webhook handler
# when a CreditMemo is created/updated/deleted in QBO.
#
# Data shape (mirrors pull_qbo_credits' CreditMemo branch):
#   billing.customer_payments stores BOTH payments and credit memos in one
#   table, distinguished by `type` ('payment' | 'credit_memo'). Credit memos
#   are stored with qbo_payment_id = "CM-{CreditMemoId}" so they don't
#   collide with payment IDs.
#
# Idempotent — safe for Intuit re-deliveries. Recalls
# billing.recheck_invoice_status() for any invoices the credit memo links
# to (since their balance / credit_review status may have flipped).

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


def main(qbo_credit_memo_id: str, operation: str = ""):
    """
    Args:
      qbo_credit_memo_id: Required. QBO Id of the CreditMemo (raw, no CM- prefix).
      operation:          Optional webhook operation hint
                          ("Create" | "Update" | "Delete" | "Void").

    Returns:
      {"status": "ok", ...}                normal upsert path
      {"status": "deleted", ...}           QBO returned 404 or operation=Delete
      {"status": "error", ...}
    """
    if not qbo_credit_memo_id:
        return {"status": "error", "error": "qbo_credit_memo_id required"}

    op = (operation or "").lower()
    print(f"=== refresh_credit_memo {qbo_credit_memo_id} (op={op or 'manual'}) ===")
    access_token, realm_id = refresh_qbo_token()

    # Internal storage key — distinguishes credit memos from payments in the
    # shared billing.customer_payments table.
    storage_id = f"CM-{qbo_credit_memo_id}"

    resp = qbo_get(f"creditmemo/{qbo_credit_memo_id}", access_token, realm_id)

    # 404 = hard delete in QBO. Treat the cache row as zero-credit
    # (unapplied_amt = 0) so it stops contributing to credit_review,
    # but keep the row for forensics. The trigger fan-out fires from
    # the unapplied_amt change.
    if resp.status_code == 404 or op in ("delete", "void"):
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE billing.customer_payments
                SET unapplied_amt = 0,
                    fetched_at = now()
                WHERE qbo_payment_id = %s
                RETURNING qbo_customer_id
            """, (storage_id,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
            return {
                "status": "deleted",
                "qbo_credit_memo_id": qbo_credit_memo_id,
                "storage_id": storage_id,
                "affected_customer_id": row[0] if row else None,
            }
        finally:
            conn.close()

    if not resp.ok:
        return {
            "status": "error",
            "error": f"QBO fetch failed: {resp.status_code}",
            "detail": resp.text[:200],
        }

    qbo_cm = (resp.json() or {}).get("CreditMemo")
    if not qbo_cm:
        return {"status": "error", "error": "QBO returned no CreditMemo"}

    customer_ref = qbo_cm.get("CustomerRef") or {}
    qbo_customer_id = customer_ref.get("value")
    total_amt = float(qbo_cm.get("TotalAmt") or 0)
    # CreditMemo's unapplied portion is RemainingCredit (NOT UnappliedAmt
    # like Payment). This is the QBO-canonical name.
    unapplied_amt = float(qbo_cm.get("RemainingCredit") or 0)
    txn_date = qbo_cm.get("TxnDate")
    ref_num = qbo_cm.get("DocNumber")
    memo = qbo_cm.get("PrivateNote")
    qbo_last_updated = parse_qbo_timestamp(
        (qbo_cm.get("MetaData") or {}).get("LastUpdatedTime")
    )

    # Linked invoices — credit memos can be applied to multiple invoices
    # via Line[].LinkedTxn. Recheck each since their balance/status may
    # have flipped.
    linked_invoice_ids = []
    for line in qbo_cm.get("Line") or []:
        for linked_txn in line.get("LinkedTxn") or []:
            if linked_txn.get("TxnType") == "Invoice":
                inv_id = linked_txn.get("TxnId")
                if inv_id and inv_id not in linked_invoice_ids:
                    linked_invoice_ids.append(inv_id)

    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            INSERT INTO billing.customer_payments
              (qbo_payment_id, qbo_customer_id, type, total_amt, unapplied_amt,
               txn_date, ref_num, memo,
               payment_method_id, payment_method_name,
               raw, fetched_at,
               qbo_last_updated_time, sync_state, sync_state_changed_at)
            VALUES (%s, %s, 'credit_memo', %s, %s, %s, %s, %s,
                    NULL, NULL,
                    %s::jsonb, now(),
                    %s, 'synced', now())
            ON CONFLICT (qbo_payment_id) DO UPDATE SET
              qbo_customer_id       = EXCLUDED.qbo_customer_id,
              total_amt             = EXCLUDED.total_amt,
              unapplied_amt         = EXCLUDED.unapplied_amt,
              txn_date              = EXCLUDED.txn_date,
              ref_num               = EXCLUDED.ref_num,
              memo                  = EXCLUDED.memo,
              raw                   = EXCLUDED.raw,
              fetched_at            = now(),
              qbo_last_updated_time = EXCLUDED.qbo_last_updated_time,
              sync_state            = 'synced',
              sync_state_changed_at = now(),
              sync_error            = NULL
            RETURNING *
        """, (
            storage_id, qbo_customer_id, total_amt, unapplied_amt,
            txn_date, ref_num, memo, _dumps(qbo_cm), qbo_last_updated,
        ))
        upserted = cur.fetchone()

        # Mirror payment_invoice_links from CreditMemo's LinkedTxn entries.
        # Same pattern as pull_qbo_credits' upsert_links_from_raw, scoped
        # to just this one credit memo. Skips invoices we don't track.
        cur.execute("SELECT qbo_invoice_id FROM billing.invoices")
        known_invoice_ids = {r["qbo_invoice_id"] for r in cur.fetchall()}

        links_written = 0
        for line in qbo_cm.get("Line") or []:
            amount = line.get("Amount") or 0
            if amount <= 0:
                continue
            for lt in (line.get("LinkedTxn") or []):
                if lt.get("TxnType") != "Invoice":
                    continue
                inv_id = str(lt.get("TxnId") or "")
                if not inv_id or inv_id not in known_invoice_ids:
                    continue
                cur.execute("""
                    INSERT INTO billing.payment_invoice_links
                      (payment_id, invoice_id, amount, applied_via, applied_at)
                    VALUES (%s, %s, %s, 'external_qbo',
                            COALESCE(%s::timestamptz, now()))
                    ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
                      amount = EXCLUDED.amount
                      -- preserve applied_via + applied_at on updates
                """, (storage_id, inv_id, float(amount), txn_date))
                links_written += 1

        # Recheck linked invoices explicitly. This duplicates work the new
        # trg_recheck_credits_on_payment_change trigger does (since the
        # upsert above changed unapplied_amt), but the trigger fans out
        # to the customer's full open-invoice set whereas this is a
        # targeted recheck of just the linked ones. Both are idempotent.
        recheck_results = []
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
                    "qbo_invoice_id": inv_id, "error": str(e)[:200],
                })

        conn.commit()
        cur.close()

        return {
            "status": "ok",
            "qbo_credit_memo_id": qbo_credit_memo_id,
            "storage_id": storage_id,
            "qbo_customer_id": qbo_customer_id,
            "total_amt": total_amt,
            "unapplied_amt": unapplied_amt,
            "links_written": links_written,
            "linked_invoices_rechecked": recheck_results,
        }
    finally:
        conn.close()
