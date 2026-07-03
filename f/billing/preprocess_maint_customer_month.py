# requirements:
# psycopg2-binary
# requests
# wmill

# f/billing/preprocess_maint_customer_month
#
# Pre-process one maintenance customer-month after its QBO invoice(s) link:
# apply the customer's unapplied maint credits in QBO, stamp the billing
# periods, and project processing_status (needs_review | ready_to_process).
#
# Module: docs/flows/monthly-maintenance-billing/index.md
# Status: [active]
# Concurrency key: qbo_writer (concurrent_limit 1 — writes QBO Payments)
#
# Triggered by:
#   - f/billing/drain_maint_preprocess_queue (serial queue drainer; the
#     billing.invoices link trigger enqueues customer-months as invoices land)
#   - manual (single customer re-run after fixing a credit problem)
#
# Tables touched:
#   billing_audit.task_billing_periods   [r/w]  the customer-month's periods;
#                                               stamps pre_processed_at,
#                                               credits_applied, credit_error
#   billing.invoices                     [read] linked-invoice sanity only
#   (projection fn touches processing_status/needs_review_reason)
#
# External APIs:
#   - QBO: query Payment/CreditMemo/Invoice per customer; POST payment
#     (credit application). NO invoice /send here — sending belongs to the
#     send step; auto-emailing would mark EmailSent and let the paid+sent
#     auto-promote skip an unreviewed hold.
#
# Why this exists:
#   Credits used to be applied inside the monthly_autopay flow by
#   f/billing/apply_maint_credits, which (a) scanned EVERY QBO Payment and
#   CreditMemo (10k-row pages) once per run, (b) emailed fully-covered
#   invoices as a side effect, and (c) left no per-period marker, so
#   "preprocessed" wasn't a real state. This script is the per-customer,
#   no-email, stamped version that the queue drainer runs one at a time as
#   invoices link (2026-07 pipeline; see task_billing_periods.processing_status).

import calendar
import json
import psycopg2
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"


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


def qbo_query(q, access_token, realm_id):
    r = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": q}, timeout=60,
    )
    if not r.ok:
        raise Exception(f"QBO query failed: {r.text[:300]}")
    return r.json().get("QueryResponse", {})


def apply_customer_credits(qbo_customer_id, target_date, access_token, realm_id, dry_run):
    """Apply the customer's unapplied maint Payments + CreditMemos to their
    open month-end maintenance invoices. Customer-scoped queries (never the
    global scan) and NO invoice email side effect."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json", "Content-Type": "application/json",
    }
    applied = []

    invs = qbo_query(
        f"SELECT * FROM Invoice WHERE CustomerRef = '{qbo_customer_id}' AND Balance > '0'",
        access_token, realm_id,
    ).get("Invoice", [])
    maint_invs = [i for i in invs if i.get("TxnDate") == target_date]
    if not maint_invs:
        return applied  # nothing open on the month-end date -> nothing to apply

    payments = qbo_query(
        f"SELECT * FROM Payment WHERE CustomerRef = '{qbo_customer_id}'",
        access_token, realm_id,
    ).get("Payment", [])
    maint_payments = [
        p for p in payments
        if float(p.get("UnappliedAmt", 0) or 0) > 0
        and "maint" in (p.get("PrivateNote", "") or "").lower()
    ]
    for payment in maint_payments:
        unapplied = float(payment.get("UnappliedAmt", 0))
        lines = [ln for ln in payment.get("Line", []) if ln.get("LinkedTxn")]
        remaining = unapplied
        targets = []
        for inv in maint_invs:
            if remaining <= 0:
                break
            bal = float(inv.get("Balance", 0))
            if bal <= 0:
                continue
            amt = min(remaining, bal)
            lines.append({"Amount": amt,
                          "LinkedTxn": [{"TxnId": inv.get("Id"), "TxnType": "Invoice"}]})
            targets.append({"invoice_id": inv.get("Id"), "doc_number": inv.get("DocNumber"),
                            "amount": amt})
            inv["Balance"] = bal - amt  # keep local view current for the next credit
            remaining -= amt
        if not targets:
            continue
        entry = {"kind": "payment", "payment_id": payment.get("Id"),
                 "unapplied_before": unapplied, "applied_to": targets}
        if not dry_run:
            resp = requests.post(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                headers=headers,
                json={"Id": payment.get("Id"), "SyncToken": payment.get("SyncToken"),
                      "CustomerRef": {"value": qbo_customer_id},
                      "TotalAmt": payment.get("TotalAmt"), "sparse": True, "Line": lines},
                timeout=60,
            )
            if not resp.ok:
                raise Exception(f"payment apply failed: {resp.text[:300]}")
        applied.append(entry)

    memos = qbo_query(
        f"SELECT * FROM CreditMemo WHERE CustomerRef = '{qbo_customer_id}' AND Balance > '0'",
        access_token, realm_id,
    ).get("CreditMemo", [])
    maint_memos = [
        cm for cm in memos
        if float(cm.get("RemainingCredit", 0) or 0) > 0
        and "maint" in (cm.get("PrivateNote", "") or "").lower()
    ]
    for cm in maint_memos:
        remaining = float(cm.get("RemainingCredit", 0) or 0)
        targets = []
        for inv in maint_invs:
            if remaining <= 0:
                break
            bal = float(inv.get("Balance", 0))
            if bal <= 0:
                continue
            amt = min(remaining, bal)
            targets.append({"invoice_id": inv.get("Id"), "doc_number": inv.get("DocNumber"),
                            "amount": amt})
            inv["Balance"] = bal - amt
            remaining -= amt
        if not targets:
            continue
        entry = {"kind": "credit_memo", "credit_memo_id": cm.get("Id"),
                 "credit_memo_doc": cm.get("DocNumber"), "applied_to": targets}
        if not dry_run:
            resp = requests.post(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                headers=headers,
                json={"TotalAmt": 0, "CustomerRef": {"value": qbo_customer_id},
                      "Line": [{"Amount": t["amount"],
                                "LinkedTxn": [{"TxnId": t["invoice_id"], "TxnType": "Invoice"},
                                              {"TxnId": cm.get("Id"), "TxnType": "CreditMemo"}]}
                               for t in targets],
                      "PrivateNote": f"Auto-applied maint credit memo {cm.get('DocNumber')}"},
                timeout=60,
            )
            if not resp.ok:
                raise Exception(f"credit memo apply failed: {resp.text[:300]}")
        applied.append(entry)

    return applied


def main(qbo_customer_id: str, billing_month: str, dry_run: bool = True):
    """billing_month: 'YYYY-MM' or 'YYYY-MM-01'."""
    month = billing_month[:7] + "-01"
    year, mon = int(month[:4]), int(month[5:7])
    target_date = f"{year}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"

    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*), count(*) FILTER (WHERE qbo_invoice_id IS NOT NULL)
               FROM billing_audit.task_billing_periods
               WHERE qbo_customer_id = %s AND billing_month = %s
                 AND locked_at IS NULL""",
            (qbo_customer_id, month),
        )
        total, linked = cur.fetchone()
        if total == 0 or linked == 0:
            return {"customer": qbo_customer_id, "month": month,
                    "action": "no linked periods — nothing to preprocess"}

        access_token, realm_id = refresh_qbo_token()
        credit_error = None
        credits = []
        try:
            credits = apply_customer_credits(
                qbo_customer_id, target_date, access_token, realm_id, dry_run)
        except Exception as e:
            credit_error = str(e)[:500]

        if dry_run:
            return {"customer": qbo_customer_id, "month": month, "dry_run": True,
                    "linked_periods": linked, "would_apply_credits": credits,
                    "credit_error": credit_error}

        if credit_error:
            # sticky operational hold; only a clean re-run clears it
            cur.execute(
                """UPDATE billing_audit.task_billing_periods
                   SET processing_status = 'needs_review',
                       needs_review_reason = 'credit_error',
                       pre_processed_at = now(),
                       notes = left(coalesce(notes || ' | ', '') || 'credit_error: ' || %s, 2000),
                       updated_at = now()
                   WHERE qbo_customer_id = %s AND billing_month = %s
                     AND locked_at IS NULL AND processing_status <> 'processed'""",
                (credit_error, qbo_customer_id, month),
            )
        else:
            cur.execute(
                """UPDATE billing_audit.task_billing_periods
                   SET pre_processed_at = now(),
                       credits_applied = %s::jsonb,
                       needs_review_reason = CASE WHEN needs_review_reason = 'credit_error'
                                                  THEN NULL ELSE needs_review_reason END,
                       updated_at = now()
                   WHERE qbo_customer_id = %s AND billing_month = %s
                     AND locked_at IS NULL AND processing_status <> 'processed'""",
                (json.dumps(credits), qbo_customer_id, month),
            )
        cur.execute(
            "SELECT billing_audit.project_maint_processing_status(%s, %s)",
            (month, qbo_customer_id),
        )
        changed = cur.fetchone()[0]
        conn.commit()
        return {"customer": qbo_customer_id, "month": month, "dry_run": False,
                "linked_periods": linked, "credits_applied": credits,
                "credit_error": credit_error, "rows_projected": changed}
    finally:
        conn.close()
