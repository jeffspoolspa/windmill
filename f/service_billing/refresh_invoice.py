# f/service_billing/refresh_invoice
#
# Single-invoice QBO -> Supabase refresh + WO link + status recheck.
#
# Four paths handled, all idempotent:
#
#   1. Existing invoice (UPSERT path):
#      → Upsert volatile fields, run billing.recheck_invoice_status, return
#      → ALSO try to link a matching WO (handles cases where the link
#        was missed during the original puller run)
#
#   2. New invoice (INSERT path):
#      → Full INSERT with all QBO fields (mirrors pull_qbo_invoices new-row logic)
#      → Match doc_number to public.work_orders.invoice_number, link the FK
#      → That UPDATE fires trg_pre_processing_on_link → pre_process_invoice
#        runs automatically
#
#   3. Voided in QBO (operation == "Void", or detected from response):
#      → Unlink any WO pointing at it (qbo_invoice_id → NULL) so the WO
#        falls back to awaiting_invoice (v_awaiting_invoice filters where
#        qbo_invoice_id IS NULL AND billable AND sub_total > 0)
#      → Mark billing_status = needs_review with reason invoice_voided
#      → Keep the cache row for forensics
#
#   4. Hard-deleted in QBO (404):
#      → Same as void path but reason = invoice_deleted_in_qbo

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


def looks_voided(qbo_inv):
    """Heuristic detection of a voided invoice from the QBO response.

    Used as a fallback when refresh_invoice is called manually (no
    operation hint from a webhook). QBO doesnt expose a clean
    is_voided boolean; the most reliable signals are:
      - PrivateNote contains "Voided" (QBO appends this on void)
      - TotalAmt == 0 AND Balance == 0 AND lines are zeroed/blank
    """
    private_note = (qbo_inv.get("PrivateNote") or "").lower()
    if "voided" in private_note or "void" in private_note.split():
        return True
    total = float(qbo_inv.get("TotalAmt") or 0)
    balance = float(qbo_inv.get("Balance") or 0)
    if total == 0 and balance == 0:
        # Could be voided OR could just be a zero-dollar invoice. Check
        # if there are any non-zero line items; if not, treat as voided.
        non_zero_lines = [
            li for li in qbo_inv.get("Line") or []
            if float(li.get("Amount", 0) or 0) != 0
            and li.get("DetailType") in ("SalesItemLineDetail", "DiscountLineDetail")
        ]
        if not non_zero_lines:
            return True
    return False


def upsert_invoice(conn, qbo_inv):
    """Full upsert. Returns (was_new: bool, qbo_invoice_id: str)."""
    customer_ref = qbo_inv.get("CustomerRef", {}) or {}
    line_items = parse_line_items(qbo_inv)
    qbo_invoice_id = qbo_inv.get("Id")
    qbo_last_updated = parse_qbo_timestamp(
        (qbo_inv.get("MetaData") or {}).get("LastUpdatedTime")
    )

    cur = conn.cursor()
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
    """Idempotent link. Always safe to run; no-op when link already exists."""
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
        return {"linked": False, "reason": "already linked or no matching billable WO",
                "doc_number": doc_number}
    return {
        "linked": True,
        "wo_numbers": [r["wo_number"] for r in rows],
        "doc_number": doc_number,
        "note": "trg_pre_processing_on_link will fire pre_process_invoice for the linked WO(s)",
    }


def seed_awaiting_pre_processing(conn, qbo_invoice_id):
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


def handle_voided(conn, qbo_invoice_id, qbo_inv=None, kind="voided"):
    """Common handler for both Void (kind=voided) and hard-Delete (kind=deleted).

    Both result in: WO unlinks → falls back to awaiting_invoice; cache row
    flagged in needs_review with the appropriate reason; row kept for forensics.
    The reason string is what surfaces in the UI needs-review queue.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if kind == "voided":
        reason = "invoice_voided"
        sync_err = "voided in QBO"
    else:
        reason = "invoice_deleted_in_qbo"
        sync_err = "deleted in QBO"

    # Unlink any WOs pointing at this invoice. The unlink does NOT fire
    # pre-processing (trigger fires on null→not-null only). The WO appears
    # back in v_awaiting_invoice immediately because that view filters on
    # qbo_invoice_id IS NULL AND billable AND sub_total > 0 AND NOT skipped.
    cur.execute("""
        UPDATE public.work_orders
        SET qbo_invoice_id = NULL
        WHERE qbo_invoice_id = %s
        RETURNING wo_number
    """, (qbo_invoice_id,))
    unlinked_wos = [r["wo_number"] for r in cur.fetchall()]

    # For void path, we have the QBO response — capture the latest state
    # in raw alongside the void marker. For delete path, raw stays as-was.
    if qbo_inv:
        cur.execute("""
            UPDATE billing.invoices
            SET billing_status      = 'needs_review',
                needs_review_reason = %s,
                sync_state          = 'synced',
                sync_state_changed_at = now(),
                sync_error          = %s,
                balance             = 0,
                total_amt           = COALESCE(%s, total_amt),
                raw                 = %s::jsonb,
                fetched_at          = now()
            WHERE qbo_invoice_id    = %s
        """, (
            reason, sync_err,
            float(qbo_inv.get("TotalAmt") or 0) if qbo_inv else None,
            _dumps(qbo_inv) if qbo_inv else None,
            qbo_invoice_id,
        ))
    else:
        cur.execute("""
            UPDATE billing.invoices
            SET billing_status      = 'needs_review',
                needs_review_reason = %s,
                sync_state          = 'synced',
                sync_state_changed_at = now(),
                sync_error          = %s,
                fetched_at          = now()
            WHERE qbo_invoice_id    = %s
        """, (reason, sync_err, qbo_invoice_id))
    affected = cur.rowcount
    cur.close()
    conn.commit()

    return {
        "kind": kind,
        "reason": reason,
        "unlinked_wos": unlinked_wos,
        "rows_marked": affected,
    }


def main(qbo_invoice_id: str, operation: str = ""):
    """
    Args:
      qbo_invoice_id: Required. QBO Id of the invoice to refresh.
      operation:      Optional. Webhook operation hint
                      ("Create" | "Update" | "Delete" | "Void" | "Emailed").
                      When provided by the webhook handler, drives the
                      void/delete branch directly. When called manually
                      without it, we fall back to detecting void from the
                      response heuristically.

    Returns one of:
      {"status": "ok", ...}        normal upsert path
      {"status": "voided", ...}    invoice voided in QBO
      {"status": "deleted", ...}   invoice hard-deleted in QBO (404)
      {"status": "error", ...}
    """
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}

    op = (operation or "").lower()
    print(f"=== refresh_invoice {qbo_invoice_id} (op={op or 'manual'}) ===")
    access_token, realm_id = refresh_qbo_token()

    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)

    # 404 = hard delete (rare, requires admin in QBO)
    if resp.status_code == 404:
        conn = get_db_conn()
        try:
            result = handle_voided(conn, qbo_invoice_id, qbo_inv=None, kind="deleted")
            return {"status": "deleted", "qbo_invoice_id": qbo_invoice_id, **result}
        finally:
            conn.close()

    if not resp.ok:
        return {
            "status": "error",
            "error": f"QBO fetch failed: {resp.status_code}",
            "detail": resp.text[:200],
        }

    qbo_inv = (resp.json() or {}).get("Invoice")
    if not qbo_inv:
        return {"status": "error", "error": "QBO returned no Invoice"}

    # Void detection: webhook operation == "Void" is authoritative; otherwise
    # fall back to the heuristic for manual invocations.
    is_voided = (op == "void") or looks_voided(qbo_inv)

    if is_voided:
        conn = get_db_conn()
        try:
            # Make sure the row exists in cache before flagging it. If it's
            # a brand-new void event for an invoice we never cached, do an
            # initial upsert so the row is present, then flag it.
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM billing.invoices WHERE qbo_invoice_id = %s",
                        (qbo_invoice_id,))
            exists = cur.fetchone() is not None
            cur.close()
            if not exists:
                upsert_invoice(conn, qbo_inv)
                conn.commit()

            result = handle_voided(conn, qbo_invoice_id, qbo_inv=qbo_inv, kind="voided")
            return {"status": "voided", "qbo_invoice_id": qbo_invoice_id, **result}
        finally:
            conn.close()

    # Normal upsert path
    conn = get_db_conn()
    try:
        was_new, qbo_invoice_id = upsert_invoice(conn, qbo_inv)
        conn.commit()

        # Always try the WO link — idempotent, no-op when link exists.
        link_result = link_to_work_order(conn, qbo_invoice_id, qbo_inv.get("DocNumber"))
        conn.commit()

        seeded = 0
        if was_new and link_result.get("linked"):
            seeded = seed_awaiting_pre_processing(conn, qbo_invoice_id)
            conn.commit()

        if was_new:
            print(f"  new invoice — link={link_result.get('linked')} seeded={seeded}")
        elif link_result.get("linked"):
            print(f"  existing invoice — backfilled WO link to {link_result.get('wo_numbers')}")

        recheck = None
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if not (was_new and link_result.get("linked")):
            try:
                cur.execute("SELECT billing.recheck_invoice_status(%s) AS r",
                            (qbo_invoice_id,))
                recheck = cur.fetchone()["r"]
            except Exception as e:
                print(f"  recheck skipped: {e}")
                conn.rollback()
        cur.close()
        conn.commit()

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM billing.invoices WHERE qbo_invoice_id = %s",
                    (qbo_invoice_id,))
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
