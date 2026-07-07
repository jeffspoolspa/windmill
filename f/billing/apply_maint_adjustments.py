"""
f/billing/apply_maint_adjustments

Write the review workbench's draft adjustments onto a QBO invoice as NEGATIVE
DISCOUNT LINES — one per adjustment, referencing the existing 'DISCOUNT'
service item (QBO Item Id 72). The original chem/labor lines stay intact, so
the ION record of what was sold survives and reconcile keeps passing; the
customer sees exactly what was comped and why in the line description.

    DISCOUNT — SALT 40LB: storm goodwill        -$20.00

Idempotent: an incoming adjustment whose (description, amount) already exists
as a DISCOUNT line on the invoice is skipped — a retried job never
double-discounts. After a successful write the invoice cache row is refreshed
(f.service_billing.refresh_invoice) so the app reflects the new total at once.

Called by /api/maintenance-billing/adjustments when the reviewer hits
Approve with drafts pending (batch: all of one invoice's adjustments in one
QBO update). Concurrency: qbo_api (shared registry).

adjustments = [{"item_name": "SALT 40LB", "amount": 20.0, "reason": "storm goodwill"}]
"""

import psycopg2
import requests
import wmill
from f.service_billing.refresh_invoice import main as refresh_invoice
from f.service_billing.refresh_invoice import refresh_qbo_token

DISCOUNT_ITEM_ID = "72"  # existing 'DISCOUNT' Service item in QBO
QBO_BASE = "https://quickbooks.api.intuit.com/v3/company"


def qbo_query(q, access_token, realm_id):
    r = requests.get(
        f"{QBO_BASE}/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": q}, timeout=60,
    )
    if not r.ok:
        raise Exception(f"QBO query failed: {r.text[:300]}")
    return r.json().get("QueryResponse", {})


def main(qbo_invoice_id: str, adjustments: list, dry_run: bool = True):
    if not adjustments:
        return {"skipped": "no adjustments"}
    for a in adjustments:
        amt = float(a.get("amount") or 0)
        if amt <= 0:
            raise Exception(f"adjustment amount must be positive dollars-off: {a}")
        if not (a.get("reason") or "").strip():
            raise Exception(f"adjustment reason required: {a}")

    access_token, realm_id = refresh_qbo_token()
    invs = qbo_query(f"SELECT * FROM Invoice WHERE Id = '{qbo_invoice_id}'",
                     access_token, realm_id).get("Invoice", [])
    if not invs:
        raise Exception(f"invoice {qbo_invoice_id} not found in QBO")
    inv = invs[0]

    existing = inv.get("Line", [])
    existing_discounts = {
        ((li.get("Description") or "").strip(), round(float(li.get("Amount") or 0), 2))
        for li in existing
        if li.get("DetailType") == "SalesItemLineDetail"
        and (li.get("SalesItemLineDetail") or {}).get("ItemRef", {}).get("value") == DISCOUNT_ITEM_ID
    }

    new_lines, skipped = [], []
    for a in adjustments:
        amt = round(float(a["amount"]), 2)
        desc = f"DISCOUNT — {a.get('item_name', 'invoice')}: {a['reason'].strip()}"
        if (desc, -amt) in existing_discounts:
            skipped.append(desc)
            continue
        line = {
            "DetailType": "SalesItemLineDetail",
            "Amount": -amt,
            "Description": desc,
            "SalesItemLineDetail": {
                "ItemRef": {"value": DISCOUNT_ITEM_ID, "name": "DISCOUNT"},
                "Qty": 1,
                "UnitPrice": -amt,
            },
        }
        # keep the invoice's class (maintenance) on the discount line too
        cls = next((li.get("SalesItemLineDetail", {}).get("ClassRef")
                    for li in existing
                    if li.get("DetailType") == "SalesItemLineDetail"
                    and li.get("SalesItemLineDetail", {}).get("ClassRef")), None)
        if cls:
            line["SalesItemLineDetail"]["ClassRef"] = cls
        new_lines.append(line)

    if not new_lines:
        return {"invoice": qbo_invoice_id, "applied": 0, "skipped_existing": skipped}

    body = {
        "Id": inv["Id"],
        "SyncToken": inv["SyncToken"],
        "sparse": True,
        "Line": existing + new_lines,  # full Line array: existing + new discounts
    }
    if dry_run:
        return {"invoice": qbo_invoice_id, "dry_run": True,
                "would_apply": [l["Description"] for l in new_lines],
                "skipped_existing": skipped}

    resp = requests.post(
        f"{QBO_BASE}/{realm_id}/invoice",
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/json", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if not resp.ok:
        raise Exception(f"discount write failed for invoice {qbo_invoice_id}: {resp.text[:400]}")
    updated = resp.json().get("Invoice", {})

    # audit ledger: one row per applied adjustment (reason recorded); the
    # unique key mirrors the QBO-line identity so retries never double-record
    sb = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=sb["host"], port=sb.get("port", 6543),
                            dbname=sb.get("dbname", "postgres"), user=sb["user"],
                            password=sb["password"], sslmode=sb.get("sslmode", "require"))
    try:
        with conn, conn.cursor() as cur:
            for a in adjustments:
                desc = f"DISCOUNT — {a.get('item_name', 'invoice')}: {a['reason'].strip()}"
                if desc in skipped:
                    continue
                cur.execute(
                    """INSERT INTO billing_audit.invoice_adjustments
                         (qbo_invoice_id, doc_number, item_name, amount_usd, reason)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (qbo_invoice_id, item_name, amount_usd, reason) DO NOTHING""",
                    (qbo_invoice_id, updated.get("DocNumber"),
                     a.get("item_name", "invoice"), round(float(a["amount"]), 2),
                     a["reason"].strip()),
                )
    finally:
        conn.close()

    # reflect the new lines/total in billing.invoices immediately
    refresh = refresh_invoice(qbo_invoice_id)

    return {
        "invoice": qbo_invoice_id,
        "applied": len(new_lines),
        "skipped_existing": skipped,
        "new_total": updated.get("TotalAmt"),
        "new_balance": updated.get("Balance"),
        "cache_refresh": refresh if isinstance(refresh, (str, int)) else "ok",
    }
