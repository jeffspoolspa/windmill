# f/service_billing/refresh_customer_credits
#
# Single-customer credit refresh. Pulls Payments + CreditMemos for one
# customer from QBO, upserts into billing.customer_payments, mirrors
# LinkedTxn into billing.payment_invoice_links, then RUNS
# billing.recheck_invoice_status on every non-terminal invoice for this
# customer (so freshly-applied credits clear the credit_review flag on
# their target invoices).
#
# Returns the fresh applicable credits AND a map of invoice patches for
# invoices whose billing_status or needs_review_reason changed. The UI
# uses the patch map to update other cards on screen for the same
# customer without a full queue refresh.

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

STALE_DAYS = 180


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _dumps(obj):
    return json.dumps(obj, default=_json_default)


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


def qbo_query_all(query, entity, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    results = []
    start = 1
    while True:
        paged = f"{query} STARTPOSITION {start} MAXRESULTS 1000"
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers, params={"query": paged, "minorversion": 65}, timeout=30,
        )
        if not resp.ok:
            raise Exception(f"QBO query failed ({entity}): {resp.status_code} - {resp.text[:200]}")
        batch = resp.json().get("QueryResponse", {}).get(entity, []) or []
        results.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    return results


def upsert_payment(cur, row, now):
    cur.execute("""
        INSERT INTO billing.customer_payments
            (qbo_payment_id, qbo_customer_id, type, unapplied_amt,
             total_amt, txn_date, ref_num, memo,
             payment_method_id, payment_method_name,
             raw, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (qbo_payment_id) DO UPDATE SET
            qbo_customer_id = EXCLUDED.qbo_customer_id,
            unapplied_amt = EXCLUDED.unapplied_amt,
            total_amt = EXCLUDED.total_amt,
            txn_date = EXCLUDED.txn_date,
            ref_num = EXCLUDED.ref_num,
            memo = EXCLUDED.memo,
            payment_method_id = COALESCE(EXCLUDED.payment_method_id, billing.customer_payments.payment_method_id),
            payment_method_name = COALESCE(EXCLUDED.payment_method_name, billing.customer_payments.payment_method_name),
            raw = EXCLUDED.raw,
            fetched_at = EXCLUDED.fetched_at
    """, (
        row["qbo_payment_id"], row["qbo_customer_id"], row["type"],
        row["unapplied_amt"], row["total_amt"], row["txn_date"],
        row["ref_num"], row["memo"],
        row.get("payment_method_id"), row.get("payment_method_name"),
        _dumps(row["raw"]), now,
    ))


def upsert_links_from_raw(cur, payment_id, raw, known_invoice_ids, txn_date):
    written = 0
    lines = (raw or {}).get("Line") or []
    for line in lines:
        amount = line.get("Amount") or 0
        if amount <= 0:
            continue
        for lt in (line.get("LinkedTxn") or []):
            if lt.get("TxnType") != "Invoice":
                continue
            invoice_id = str(lt.get("TxnId") or "")
            if not invoice_id or invoice_id not in known_invoice_ids:
                continue
            cur.execute("""
                INSERT INTO billing.payment_invoice_links
                    (payment_id, invoice_id, amount, applied_via, applied_at)
                VALUES (%s, %s, %s, 'external_qbo', COALESCE(%s::timestamptz, now()))
                ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
                    amount = EXCLUDED.amount
            """, (payment_id, invoice_id, float(amount), txn_date))
            written += 1
    return written


def main(qbo_customer_id: str, lookback_days: int = 365,
         access_token: str = None, realm_id: str = None):
    """
    Returns:
      {
        "status": "ok",
        "credits": [...applicable credits...],
        "links_written": N,
        "invoice_patches": { qbo_invoice_id: {...fresh row...}, ... },
        "rechecked_invoices": N,
        "changed_invoices": N,
      }

    access_token/realm_id: pass a caller's already-refreshed token to reuse it.
    QBO refresh tokens rotate on every refresh (CLAUDE.md burn warning), so an
    in-process caller (e.g. pre_process_invoice's freshness gate) that already
    holds a token passes it here instead of triggering a second rotation.
    """
    if not qbo_customer_id:
        return {"status": "error", "error": "qbo_customer_id required"}

    print(f"=== refresh_customer_credits customer={qbo_customer_id} ===")
    if not (access_token and realm_id):
        access_token, realm_id = refresh_qbo_token()

    now = datetime.now(timezone.utc)
    qbo_cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    ui_cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()

    pay_query = (
        f"SELECT * FROM Payment "
        f"WHERE CustomerRef = '{qbo_customer_id}' "
        f"AND TxnDate >= '{qbo_cutoff}'"
    )
    cm_query = f"SELECT * FROM CreditMemo WHERE CustomerRef = '{qbo_customer_id}'"

    payments = qbo_query_all(pay_query, "Payment", access_token, realm_id)
    credit_memos = qbo_query_all(cm_query, "CreditMemo", access_token, realm_id)
    print(f"  QBO returned: {len(payments)} payments, {len(credit_memos)} credit memos")

    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT qbo_invoice_id FROM billing.invoices")
        known_invoice_ids = {r["qbo_invoice_id"] for r in cur.fetchall()}

        links_written = 0

        for p in payments:
            pmref = p.get("PaymentMethodRef") or {}
            row = {
                "qbo_payment_id": p.get("Id"),
                # Trust the payload's CustomerRef over the caller's arg —
                # after a QBO customer merge the payment may belong to a
                # different (surviving) customer than the one being refreshed.
                "qbo_customer_id": (p.get("CustomerRef") or {}).get("value") or qbo_customer_id,
                "type": "payment",
                "unapplied_amt": float(p.get("UnappliedAmt") or 0),
                "total_amt": float(p.get("TotalAmt") or 0),
                "txn_date": p.get("TxnDate"),
                "ref_num": p.get("PaymentRefNum"),
                "memo": p.get("PrivateNote"),
                "payment_method_id": pmref.get("value"),
                "payment_method_name": None,
                "raw": p,
            }
            upsert_payment(cur, row, now)
            links_written += upsert_links_from_raw(
                cur, row["qbo_payment_id"], p, known_invoice_ids, row["txn_date"],
            )

        for cm in credit_memos:
            row = {
                "qbo_payment_id": f"CM-{cm.get('Id')}",
                "qbo_customer_id": (cm.get("CustomerRef") or {}).get("value") or qbo_customer_id,
                "type": "credit_memo",
                "unapplied_amt": float(cm.get("RemainingCredit") or 0),
                "total_amt": float(cm.get("TotalAmt") or 0),
                "txn_date": cm.get("TxnDate"),
                "ref_num": cm.get("DocNumber"),
                "memo": cm.get("PrivateNote"),
                "payment_method_id": None,
                "payment_method_name": None,
                "raw": cm,
            }
            upsert_payment(cur, row, now)
            links_written += upsert_links_from_raw(
                cur, row["qbo_payment_id"], cm, known_invoice_ids, row["txn_date"],
            )

        # Find all non-terminal invoices for this customer and run
        # recheck_invoice_status on each. Credits changing (applied,
        # reduced, or fully exhausted) can clear credit_review flags on
        # multiple invoices for the same customer. Terminal 'processed'
        # invoices are skipped by the function itself.
        cur.execute("""
            SELECT qbo_invoice_id
            FROM billing.invoices
            WHERE qbo_customer_id = %s
              AND billing_status IN ('needs_review', 'ready_to_process',
                                     'awaiting_pre_processing')
        """, (qbo_customer_id,))
        candidate_ids = [r["qbo_invoice_id"] for r in cur.fetchall()]

        invoice_patches = {}
        changed_count = 0
        for inv_id in candidate_ids:
            cur.execute("SELECT billing.recheck_invoice_status(%s) AS r", (inv_id,))
            recheck = cur.fetchone()["r"]
            if recheck.get("status") != "ok":
                continue
            if recheck.get("changed"):
                changed_count += 1
            # Always return the reconciled state so the UI can patch with
            # confidence even when nothing changed — avoids a "stale" UI
            # mistakenly reading an older snapshot.
            invoice_patches[inv_id] = _json_safe(recheck.get("invoice"))

        conn.commit()

        cur.execute("""
            SELECT id, qbo_payment_id, type, unapplied_amt, total_amt,
                   txn_date, ref_num, memo
            FROM billing.customer_payments
            WHERE qbo_customer_id = %s
              AND unapplied_amt > 0
              AND (txn_date IS NULL OR txn_date >= %s)
              AND (memo IS NULL OR memo !~* 'maint')
            ORDER BY txn_date ASC NULLS LAST
        """, (qbo_customer_id, ui_cutoff))
        rows = cur.fetchall()
        cur.close()

        credits = []
        for r in rows:
            d = dict(r)
            credits.append({
                "id": d["id"],
                "qbo_payment_id": d["qbo_payment_id"],
                "type": d["type"],
                "unapplied_amt": float(d["unapplied_amt"]) if d["unapplied_amt"] is not None else None,
                "total_amt": float(d["total_amt"]) if d["total_amt"] is not None else None,
                "txn_date": d["txn_date"].isoformat() if d["txn_date"] else None,
                "ref_num": d["ref_num"],
                "memo": d["memo"],
            })

        print(f"  applicable after UI filter: {len(credits)}")
        print(f"  links written: {links_written}")
        print(f"  rechecked invoices: {len(candidate_ids)} ({changed_count} changed)")
        return {
            "status": "ok",
            "qbo_customer_id": qbo_customer_id,
            "credits": credits,
            "links_written": links_written,
            "invoice_patches": invoice_patches,
            "rechecked_invoices": len(candidate_ids),
            "changed_invoices": changed_count,
            "qbo_payments_scanned": len(payments),
            "qbo_credit_memos_scanned": len(credit_memos),
        }
    finally:
        conn.close()
