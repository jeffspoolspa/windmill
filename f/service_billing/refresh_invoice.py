# f/service_billing/refresh_invoice
#
# Single-invoice QBO -> Supabase refresh + WO link + status recheck.
#
# Three paths handled, all idempotent:
#
#   1. Invoice already in cache (typical webhook update path):
#      → UPSERT volatile fields, run billing.recheck_invoice_status, return
#
#   2. Invoice NOT in cache (new-invoice webhook from QBO):
#      → INSERT row with all QBO fields (mirrors pull_qbo_invoices new-row logic)
#      → Match doc_number to public.work_orders.invoice_number, link the FK
#      → That UPDATE fires trg_pre_processing_on_link → pre_process_invoice
#        runs automatically (no extra wiring needed here)
#      → Return with link_result so the webhook handler can log it
#
#   3. Invoice deleted in QBO (404):
#      → Mark sync_state and return; reconciler will catch any stragglers
#
# Note on the matching WO not existing:
#   If QBO emits invoice.create before ION scrapes the WO, the link step is a
#   no-op. The new BEFORE trigger on work_orders (fn_link_invoice_on_wo_change)
#   handles that direction — when ION later inserts the WO with the matching
#   invoice_number, it auto-populates qbo_invoice_id and pre-processing fires
#   via the now-INSERT-aware pre_processing trigger.

import json
from datetime import date, datetime, timezone
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
    for line in qbo_inv.get("Line") or []:
        dt = line.get("DetailType")
        if dt not in ("SalesItemLineDetail", "DescriptionOnly", "SubTotalLineDetail", "DiscountLineDetail"):
            continue
        amount = float(line.get("Amount", 0) or 0)
        desc = line.get("Description", "")
        if dt == "SubTotalLineDetail":
            out.append({"item_id": None, "item_name": "Subtotal", "description": desc,
                        "qty": None, "unit_price": None, "amount": amount, "line_type": "subtotal"})
        elif dt == "DescriptionOnly":
            out.append({"item_id": None, "item_name": None, "description": desc,
                        "qty": None, "unit_price": None, "amount": amount, "line_type": "description"})
        elif dt == "DiscountLineDetail":
            d = line.get("DiscountLineDetail", {}) or {}
            out.append({"item_id": None, "item_name": "Discount", "description": desc,
                        "qty": None, "unit_price": None, "amount": amount, "line_type": "discount",
                        "percent": d.get("DiscountPercent")})
        else:
            si = line.get("SalesItemLineDetail", {}) or {}
            item_ref = si.get("ItemRef", {}) or {}
            out.append({
                "item_id": item_ref.get("value"),
                "item_name": item_ref.get("name"),
                "description": desc,
                "qty": float(si.get("Qty", 0) or 0),
                "unit_price": float(si.get("UnitPrice", 0) or 0),
                "amount": amount,
                "line_type": "item",
            })
    return out


def parse_qbo_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def upsert_invoice(conn, qbo_inv):
    """Full upsert of QBO invoice into billing.invoices.

    Returns (was_new: bool, qbo_invoice_id: str). Mirrors pull_qbo_invoices'
    upsert column list so a webhook-driven new-invoice insert produces the
    same shape as a bulk-puller insert."""
    customer_ref = qbo_inv.get("CustomerRef", {}) or {}
    line_items = parse_line_items(qbo_inv)

    qbo_invoice_id = qbo_inv.get("Id")
    qbo_last_updated = parse_qbo_timestamp(
        (qbo_inv.get("MetaData") or {}).get("LastUpdatedTime")
    )

    cur = conn.cursor()

    # Detect insert-vs-update via a quick existence check before upsert
    # so we know whether to do the WO link step afterward.
    cur.execute(
        "SELECT 1 FROM billing.invoices WHERE qbo_invoice_id = %s",
        (qbo_invoice_id,),
    )
    was_new = cur.fetchone() is None

    cur.execute("""
        INSERT INTO billing.invoices (
            qbo_invoice_id, doc_number, qbo_customer_id, customer_name,
            txn_date, due_date, total_amt, subtotal, balance, email_status,
            line_items, raw, fetched_at, qbo_last_updated_time,
            sync_state, sync_state_changed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, now(), %s,
                  'synced', now())
        ON CONFLICT (qbo_invoice_id) DO UPDATE SET
            doc_number            = EXCLUDED.doc_number,
            qbo_customer_id       = EXCLUDED.qbo_customer_id,
            customer_name         = EXCLUDED.customer_name,
            txn_date              = EXCLUDED.txn_date,
            due_date              = EXCLUDED.due_date,
            total_amt             = EXCLUDED.total_amt,
            subtotal              = EXCLUDED.subtotal,
            balance               = EXCLUDED.balance,
            email_status          = EXCLUDED.email_status,
            line_items            = EXCLUDED.line_items,
            raw                   = EXCLUDED.raw,
            fetched_at            = now(),
            qbo_last_updated_time = EXCLUDED.qbo_last_updated_time,
            sync_state            = 'synced',
            sync_state_changed_at = now(),
            sync_error            = NULL
    """, (
        qbo_invoice_id, qbo_inv.get("DocNumber"),
        customer_ref.get("value"), customer_ref.get("name"),
        qbo_inv.get("TxnDate"), qbo_inv.get("DueDate"),
        float(qbo_inv.get("TotalAmt", 0) or 0),
        qbo_invoice_subtotal(qbo_inv),
        float(qbo_inv.get("Balance", 0) or 0),
        qbo_inv.get("EmailStatus"),
        _dumps(line_items), _dumps(qbo_inv),
        qbo_last_updated,
    ))

    cur.close()
    return was_new, qbo_invoice_id


def link_to_work_order(conn, qbo_invoice_id, doc_number):
    """Find a WO whose invoice_number matches doc_number and set
    qbo_invoice_id on it. The downstream trg_pre_processing_on_link trigger
    fires on this UPDATE → kicks off pre_process_invoice automatically.

    Returns dict describing what happened so the webhook log can record it.
    """
    if not doc_number:
        return {"linked": False, "reason": "invoice has no doc_number"}

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        UPDATE public.work_orders
        SET qbo_invoice_id = %s
        WHERE invoice_number = %s
          AND billable = true
          AND qbo_invoice_id IS DISTINCT FROM %s
        RETURNING wo_number, qbo_invoice_id
    """, (qbo_invoice_id, doc_number, qbo_invoice_id))
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return {
            "linked": False,
            "reason": "no matching billable WO yet (will link when ION scrape lands)",
            "doc_number": doc_number,
        }
    return {
        "linked": True,
        "wo_numbers": [r["wo_number"] for r in rows],
        "doc_number": doc_number,
        "note": "trg_pre_processing_on_link will fire pre_process_invoice for the linked WO(s)",
    }


def seed_awaiting_pre_processing(conn, qbo_invoice_id):
    """If the invoice is now linked to a billable WO and has no billing_status,
    seed it as awaiting_pre_processing so the UI queue reflects the state
    even before pre_process_invoice writes back."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices i
        SET billing_status = 'awaiting_pre_processing'
        WHERE i.qbo_invoice_id = %s
          AND i.billing_status IS NULL
          AND EXISTS (
            SELECT 1 FROM public.work_orders w
            WHERE w.qbo_invoice_id = i.qbo_invoice_id
              AND w.billable = true
          )
    """, (qbo_invoice_id,))
    seeded = cur.rowcount
    cur.close()
    return seeded


def main(qbo_invoice_id: str):
    """
    Returns:
      {"status": "ok", "was_new": <bool>, "invoice": {...},
       "link_result": {...}, "recheck": {...}}
      {"status": "deleted", "qbo_invoice_id": "..."}  if QBO returns 404
      {"status": "error", "error": "..."}
    """
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}

    print(f"=== refresh_invoice {qbo_invoice_id} ===")
    access_token, realm_id = refresh_qbo_token()

    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)

    if resp.status_code == 404:
        # Invoice voided / deleted in QBO. Mark and return.
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE billing.invoices
                SET sync_state = 'synced',
                    sync_state_changed_at = now(),
                    sync_error = 'deleted in QBO',
                    fetched_at = now()
                WHERE qbo_invoice_id = %s
            """, (qbo_invoice_id,))
            conn.commit()
            cur.close()
        finally:
            conn.close()
        return {"status": "deleted", "qbo_invoice_id": qbo_invoice_id}

    if not resp.ok:
        return {
            "status": "error",
            "error": f"QBO fetch failed: {resp.status_code}",
            "detail": resp.text[:200],
        }

    qbo_inv = (resp.json() or {}).get("Invoice")
    if not qbo_inv:
        return {"status": "error", "error": "QBO returned no Invoice"}

    conn = get_db_conn()
    try:
        # 1. Upsert (handles both new-invoice and existing-invoice paths)
        was_new, qbo_invoice_id = upsert_invoice(conn, qbo_inv)
        conn.commit()

        link_result = None
        seeded = 0

        # 2. New-invoice path: try to link to a WO. If linked, the AFTER
        #    UPDATE trigger fires pre_process_invoice automatically.
        if was_new:
            link_result = link_to_work_order(conn, qbo_invoice_id, qbo_inv.get("DocNumber"))
            conn.commit()
            seeded = seed_awaiting_pre_processing(conn, qbo_invoice_id)
            conn.commit()
            print(f"  new invoice — link={link_result.get('linked')} seeded={seeded}")

        # 3. Recheck status (memo, credits, subtotal — deterministic reasons only).
        #    Skip for newly-inserted-and-linked invoices because pre_process_invoice
        #    is about to run anyway and will set the status authoritatively.
        recheck = None
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if not (was_new and link_result and link_result.get("linked")):
            try:
                cur.execute(
                    "SELECT billing.recheck_invoice_status(%s) AS r",
                    (qbo_invoice_id,),
                )
                recheck = cur.fetchone()["r"]
            except Exception as e:
                # recheck is best-effort. If the function errors (e.g.
                # invoice has no linked WO yet), continue without it.
                print(f"  recheck skipped: {e}")
                conn.rollback()
        cur.close()
        conn.commit()

        # Pull the final reconciled row for return
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM billing.invoices WHERE qbo_invoice_id = %s",
            (qbo_invoice_id,),
        )
        invoice_row = cur.fetchone()
        cur.close()

        return {
            "status": "ok",
            "qbo_invoice_id": qbo_invoice_id,
            "was_new": was_new,
            "invoice": _json_safe(dict(invoice_row) if invoice_row else None),
            "link_result": link_result,
            "seeded_awaiting_pre_processing": seeded if was_new else None,
            "recheck": _json_safe(recheck) if recheck else None,
        }
    finally:
        conn.close()
