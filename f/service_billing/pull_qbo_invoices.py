# Pull QBO invoices into billing.invoices cache + link to WOs.
#
# Two modes:
#
#   Bulk (default):   pull_qbo_invoices(force_refresh=False, max_age_minutes=60, limit=None)
#       - Finds every billable WO's invoice_number that's missing from cache or stale
#       - Batch-fetches via IN-clause
#       - Upserts, links, seeds awaiting_pre_processing
#
#   Single-WO:        pull_qbo_invoices(wo_number="4746495")
#       - Fetches just that WO's invoice from QBO live
#       - Upserts + links
#       - Auto-chains to pre_process_invoice with force=True
#       - Returns combined result (use case: manual UI "Sync from QBO" button)

import requests
import wmill
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
QBO_IN_BATCH_SIZE = 200


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


def qbo_invoice_subtotal(inv: dict) -> float:
    for line in inv.get("Line", []) or []:
        if line.get("DetailType") == "SubTotalLineDetail":
            try:
                return round(float(line.get("Amount", 0) or 0), 2)
            except (TypeError, ValueError):
                pass
    total = float(inv.get("TotalAmt", 0) or 0)
    tax = float((inv.get("TxnTaxDetail") or {}).get("TotalTax", 0) or 0)
    return round(total - tax, 2)


def find_invoice_numbers_to_fetch(conn, force_refresh, max_age_minutes, limit):
    cur = conn.cursor()
    if force_refresh:
        sql = """
            SELECT DISTINCT w.invoice_number
            FROM public.work_orders w
            WHERE w.invoice_number IS NOT NULL
              AND w.billable = true
        """
        params = []
    else:
        sql = """
            SELECT DISTINCT w.invoice_number
            FROM public.work_orders w
            LEFT JOIN billing.invoices i ON i.doc_number = w.invoice_number
            WHERE w.invoice_number IS NOT NULL
              AND w.billable = true
              AND (i.qbo_invoice_id IS NULL
                   OR i.fetched_at < (now() - (%s || ' minutes')::interval))
        """
        params = [str(max_age_minutes)]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    cur.execute(sql, params if params else None)
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    return rows


def batch_fetch_invoices(invoice_numbers, access_token, realm_id):
    if not invoice_numbers:
        return {}, []
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    found = {}
    errors = []
    for i in range(0, len(invoice_numbers), QBO_IN_BATCH_SIZE):
        batch = invoice_numbers[i:i + QBO_IN_BATCH_SIZE]
        in_values = ", ".join([f"'{d}'" for d in batch])
        query = f"SELECT * FROM Invoice WHERE DocNumber IN ({in_values}) MAXRESULTS 1000"
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query?minorversion=65",
            headers=headers, params={"query": query}, timeout=60,
        )
        if not resp.ok:
            err = f"Batch {i // QBO_IN_BATCH_SIZE + 1}: HTTP {resp.status_code} - {resp.text[:300]}"
            print(f"  ERROR: {err}")
            errors.append(err)
            continue
        invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])
        for inv in invoices:
            doc_num = inv.get("DocNumber")
            if doc_num:
                found[str(doc_num)] = inv
        print(f"  batch {i // QBO_IN_BATCH_SIZE + 1}: requested {len(batch)}, returned {len(invoices)}")
    return found, errors


def transform_line_items(qbo_lines):
    out = []
    for line in qbo_lines:
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


def upsert_invoices(conn, qbo_invoices):
    if not qbo_invoices:
        return 0
    cur = conn.cursor()
    upserted = 0
    now = datetime.now(timezone.utc)
    for doc_number, inv in qbo_invoices.items():
        try:
            qbo_id = inv.get("Id")
            customer_ref = inv.get("CustomerRef", {}) or {}
            line_items = transform_line_items(inv.get("Line", []))
            cur.execute(
                """INSERT INTO billing.invoices (
                    qbo_invoice_id, doc_number, qbo_customer_id, customer_name,
                    txn_date, due_date, total_amt, subtotal, balance, email_status,
                    line_items, raw, fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (qbo_invoice_id) DO UPDATE SET
                    doc_number = EXCLUDED.doc_number,
                    qbo_customer_id = EXCLUDED.qbo_customer_id,
                    customer_name = EXCLUDED.customer_name,
                    txn_date = EXCLUDED.txn_date,
                    due_date = EXCLUDED.due_date,
                    total_amt = EXCLUDED.total_amt,
                    subtotal = EXCLUDED.subtotal,
                    balance = EXCLUDED.balance,
                    email_status = EXCLUDED.email_status,
                    line_items = EXCLUDED.line_items,
                    raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at""",
                (
                    qbo_id, str(doc_number), customer_ref.get("value"), customer_ref.get("name"),
                    inv.get("TxnDate"), inv.get("DueDate"),
                    float(inv.get("TotalAmt", 0) or 0),
                    qbo_invoice_subtotal(inv),
                    float(inv.get("Balance", 0) or 0),
                    inv.get("EmailStatus"),
                    psycopg2.extras.Json(line_items),
                    psycopg2.extras.Json(inv),
                    now,
                ),
            )
            upserted += 1
        except Exception as e:
            print(f"  upsert error for doc {doc_number}: {e}")
    conn.commit()
    cur.close()
    return upserted


def link_work_orders_to_invoices(conn):
    """Set work_orders.qbo_invoice_id from billing.invoices lookup. Idempotent."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE public.work_orders w
        SET qbo_invoice_id = sub.target_id
        FROM (
            SELECT wo.wo_number,
                   (SELECT i.qbo_invoice_id FROM billing.invoices i
                    WHERE i.doc_number = wo.invoice_number LIMIT 1) AS target_id
            FROM public.work_orders wo
            WHERE wo.invoice_number IS NOT NULL
              AND wo.billable = true
        ) sub
        WHERE w.wo_number = sub.wo_number
          AND w.qbo_invoice_id IS DISTINCT FROM sub.target_id
    """)
    linked = cur.rowcount or 0
    conn.commit()
    cur.close()
    return linked


def seed_awaiting_pre_processing(conn):
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices i
        SET billing_status = 'awaiting_pre_processing'
        WHERE i.billing_status IS NULL
          AND EXISTS (
            SELECT 1 FROM public.work_orders w
            WHERE w.qbo_invoice_id = i.qbo_invoice_id
              AND w.billable = true
          )
    """)
    seeded = cur.rowcount or 0
    conn.commit()
    cur.close()
    return seeded


def fetch_one_for_wo(conn, wo_number, access_token, realm_id):
    """Single-WO mode: look up WO + fetch its one invoice from QBO + upsert + link.

    Returns dict with status + qbo_invoice_id (or error).
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT wo_number, invoice_number FROM public.work_orders WHERE wo_number = %s",
        (wo_number,),
    )
    wo = cur.fetchone()
    cur.close()

    if not wo:
        return {"status": "error", "error": f"WO {wo_number} not found"}
    invoice_number = wo.get("invoice_number")
    if not invoice_number:
        return {"status": "error", "error": "WO has no invoice_number — office hasn't entered it in ION yet"}

    print(f"  Single-WO mode: fetching invoice {invoice_number} for WO {wo_number}")
    found, errors = batch_fetch_invoices([invoice_number], access_token, realm_id)
    if errors:
        return {"status": "error", "error": f"QBO fetch errors: {errors}"}
    if not found:
        return {
            "status": "error",
            "error": f"Invoice {invoice_number} not found in QBO. Check the number is correct or the invoice still exists.",
        }

    upsert_invoices(conn, found)
    qbo_inv = list(found.values())[0]
    qbo_invoice_id = qbo_inv.get("Id")

    # Link this WO directly (faster + targeted vs full reconciliation)
    cur = conn.cursor()
    cur.execute(
        "UPDATE public.work_orders SET qbo_invoice_id = %s WHERE wo_number = %s "
        "AND qbo_invoice_id IS DISTINCT FROM %s",
        (qbo_invoice_id, wo_number, qbo_invoice_id),
    )
    conn.commit(); cur.close()

    seed_awaiting_pre_processing(conn)
    return {"status": "linked", "qbo_invoice_id": qbo_invoice_id, "invoice_number": invoice_number}


def main(force_refresh: bool = False, max_age_minutes: int = 60,
         limit: int = None, wo_number: str = None):
    """Two modes:

       wo_number set      → single-WO sync + chain to pre_process_invoice
       wo_number None     → bulk: pull missing/stale invoices for all billable WOs
    """
    print(f"=== pull_qbo_invoices started ===")

    conn = get_db_conn()
    try:
        access_token, realm_id = refresh_qbo_token()

        # ─── SINGLE-WO MODE ────────────────────────────────────────────
        if wo_number:
            sync_result = fetch_one_for_wo(conn, wo_number, access_token, realm_id)
            if sync_result.get("status") != "linked":
                return sync_result

            qbo_invoice_id = sync_result["qbo_invoice_id"]
            print(f"  triggering pre_process_invoice (force=True) for {qbo_invoice_id}")
            try:
                pp_result = wmill.run_script_by_path(
                    "f/service_billing/pre_process_invoice",
                    {"qbo_invoice_id": qbo_invoice_id, "force": True, "bulk_all": False},
                )
            except Exception as e:
                pp_result = {"status": "error", "error": f"pre_process trigger failed: {e}"}

            return {
                "status": "success",
                "mode": "single_wo",
                "wo_number": wo_number,
                "invoice_number": sync_result.get("invoice_number"),
                "qbo_invoice_id": qbo_invoice_id,
                "pre_processing": pp_result,
            }

        # ─── BULK MODE ─────────────────────────────────────────────────
        print(f"  bulk: force_refresh={force_refresh} max_age_minutes={max_age_minutes} limit={limit}")
        invoice_numbers = find_invoice_numbers_to_fetch(conn, force_refresh, max_age_minutes, limit)
        print(f"  Found {len(invoice_numbers)} invoice numbers to fetch")

        qbo_invoices, errors, upserted = {}, [], 0
        if invoice_numbers:
            qbo_invoices, errors = batch_fetch_invoices(invoice_numbers, access_token, realm_id)
            print(f"  QBO returned {len(qbo_invoices)} invoices ({len(errors)} batch errors)")
            upserted = upsert_invoices(conn, qbo_invoices)
            print(f"  Upserted {upserted} invoices into billing.invoices")

        linked = link_work_orders_to_invoices(conn)
        seeded = seed_awaiting_pre_processing(conn)
        print(f"  Linked {linked} WO->invoice FKs; seeded {seeded} invoices to awaiting_pre_processing")

        not_found = [n for n in invoice_numbers if str(n) not in qbo_invoices]
        return {
            "status": "success" if not errors else "partial",
            "mode": "bulk",
            "to_fetch": len(invoice_numbers),
            "fetched": len(qbo_invoices),
            "upserted": upserted,
            "wo_links_updated": linked,
            "invoices_seeded_awaiting_pre_processing": seeded,
            "not_found_in_qbo": len(not_found),
            "not_found_sample": not_found[:20],
            "batch_errors": errors,
        }
    finally:
        conn.close()
