# f/service_billing/refresh_invoice
#
# Single-invoice QBO → Supabase refresh. Takes one qbo_invoice_id, fetches
# the current state from QBO, upserts the volatile fields into
# billing.invoices, runs billing.recheck_invoice_status (reconciles
# billing_status + needs_review_reason from current DB state), and returns
# the fully reconciled row.
#
# This is a READ refresh + status recheck — NOT a full pull, NOT a
# pre-process trigger. The recheck only handles deterministic reasons
# (subtotal_mismatch, credit_review); memo reasons are preserved.
#
# Used by the UI's useFreshResource hook to keep the view in sync with QBO
# when the user's attention lands on a resource. Cheap enough to call on
# every focus-change.

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


def _json_safe(obj):
    """Recursively convert Decimals/dates to JSON-safe primitives."""
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


def qbo_get(path, access_token, realm_id):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )


def qbo_invoice_subtotal(qbo_inv):
    for li in qbo_inv.get("Line") or []:
        if li.get("DetailType") == "SubTotalLineDetail":
            amt = li.get("Amount")
            if amt is not None:
                return float(amt)
    total = float(qbo_inv.get("TotalAmt") or 0)
    tax = float((qbo_inv.get("TxnTaxDetail") or {}).get("TotalTax") or 0)
    return round(total - tax, 2)


def parse_line_items(qbo_inv):
    out = []
    for li in qbo_inv.get("Line") or []:
        detail_type = li.get("DetailType")
        if detail_type == "SubTotalLineDetail":
            out.append({
                "item_id": None, "item_name": None,
                "description": li.get("Description"),
                "qty": None, "unit_price": None,
                "amount": float(li.get("Amount") or 0),
                "line_type": "subtotal",
            })
            continue
        sid = li.get("SalesItemLineDetail") or {}
        item_ref = sid.get("ItemRef") or {}
        out.append({
            "item_id": item_ref.get("value"),
            "item_name": item_ref.get("name"),
            "description": li.get("Description"),
            "qty": float(sid["Qty"]) if sid.get("Qty") is not None else None,
            "unit_price": float(sid["UnitPrice"]) if sid.get("UnitPrice") is not None else None,
            "amount": float(li.get("Amount")) if li.get("Amount") is not None else None,
            "line_type": "sales",
        })
    return out


def main(qbo_invoice_id: str):
    """
    Returns:
      {"status": "ok", "invoice": {...reconciled row...}, "recheck": {...}}
      {"status": "error", "error": "<reason>"}
    """
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}

    print(f"=== refresh_invoice {qbo_invoice_id} ===")
    access_token, realm_id = refresh_qbo_token()

    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)
    if not resp.ok:
        return {
            "status": "error",
            "error": f"QBO fetch failed: {resp.status_code}",
            "detail": resp.text[:200],
        }

    qbo_inv = (resp.json() or {}).get("Invoice")
    if not qbo_inv:
        return {"status": "error", "error": "QBO returned no Invoice"}

    balance = float(qbo_inv.get("Balance") or 0)
    total_amt = float(qbo_inv.get("TotalAmt") or 0)
    subtotal = qbo_invoice_subtotal(qbo_inv)
    email_status = qbo_inv.get("EmailStatus")
    doc_number = qbo_inv.get("DocNumber")
    txn_date = qbo_inv.get("TxnDate")
    due_date = qbo_inv.get("DueDate")
    line_items = parse_line_items(qbo_inv)

    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1. Upsert volatile fields from QBO
        cur.execute("""
            UPDATE billing.invoices
            SET balance = %s,
                total_amt = %s,
                subtotal = %s,
                email_status = %s,
                doc_number = COALESCE(%s, doc_number),
                txn_date = COALESCE(%s, txn_date),
                due_date = COALESCE(%s, due_date),
                line_items = %s::jsonb,
                raw = %s::jsonb,
                fetched_at = now()
            WHERE qbo_invoice_id = %s
        """, (
            balance, total_amt, subtotal, email_status,
            doc_number, txn_date, due_date,
            _dumps(line_items), _dumps(qbo_inv),
            qbo_invoice_id,
        ))

        if cur.rowcount == 0:
            conn.rollback()
            return {
                "status": "error",
                "error": f"invoice {qbo_invoice_id} not in billing.invoices — "
                         f"run pull_qbo_invoices first",
            }

        # 2. Reconcile status + reasons from current DB state. This reads
        # the just-updated invoice row + current customer_payments and
        # rebuilds needs_review_reason + billing_status deterministically.
        cur.execute("SELECT billing.recheck_invoice_status(%s) AS r", (qbo_invoice_id,))
        recheck = cur.fetchone()["r"]

        conn.commit()
        cur.close()

        if recheck.get("status") == "error":
            return {"status": "error", "error": recheck.get("error")}

        return {
            "status": "ok",
            "invoice": _json_safe(recheck.get("invoice")),
            "recheck": {
                "changed": recheck.get("changed"),
                "prev_billing_status": recheck.get("prev_billing_status"),
                "new_billing_status": recheck.get("new_billing_status"),
                "prev_reason": recheck.get("prev_reason"),
                "new_reason": recheck.get("new_reason"),
            },
        }
    finally:
        conn.close()
