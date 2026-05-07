# f/service_billing/refresh_invoice
#
# Single-invoice QBO -> Supabase refresh + WO link + status recheck.
#
# Callers:
#   - QBO webhook handler:    main(qbo_invoice_id, operation="...")
#                             — fetches the invoice from QBO and refreshes
#   - cdc_reconciler:         main(qbo_invoice_id, qbo_body=<cdc_entity>)
#                             — passes the body it already has from CDC,
#                               skipping the QBO GET. Single source of truth
#                               for the upsert + side effects.
#
# Concurrency: the upsert uses an OCC guard on qbo_last_updated_time, so two
# concurrent callers writing the same invoice never clobber each other —
# whichever has the newer QBO timestamp wins, the other's UPDATE is a no-op.
# Side effects (external-memo detection, WO link, status recheck) are gated
# on `did_write` so the loser doesn't trigger downstream churn either.
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
    """Heuristic detection of a voided invoice from the QBO response."""
    private_note = (qbo_inv.get("PrivateNote") or "").lower()
    if "voided" in private_note or "void" in private_note.split():
        return True
    total = float(qbo_inv.get("TotalAmt") or 0)
    balance = float(qbo_inv.get("Balance") or 0)
    if total == 0 and balance == 0:
        non_zero_lines = [
            li for li in qbo_inv.get("Line") or []
            if float(li.get("Amount", 0) or 0) != 0
            and li.get("DetailType") in ("SalesItemLineDetail", "DiscountLineDetail")
        ]
        if not non_zero_lines:
            return True
    return False


def upsert_invoice(conn, qbo_inv):
    """Upsert with OCC guard on qbo_last_updated_time.

    Returns (was_new, qbo_invoice_id, prev_memo, qbo_memo, did_write).

      was_new   — true when the row didn't exist before our SELECT
      did_write — true when the INSERT or ON CONFLICT UPDATE actually landed
                  (false when OCC blocked us — i.e. someone newer beat us).
                  Side effects (memo-lock, link, recheck) should gate on this
                  so the race loser doesn't trigger spurious downstream work.

    The OCC guard only updates when EXCLUDED.qbo_last_updated_time is strictly
    newer than the existing row's. New inserts (no conflict) always land.
    """
    customer_ref = qbo_inv.get("CustomerRef", {}) or {}
    line_items = parse_line_items(qbo_inv)
    qbo_invoice_id = qbo_inv.get("Id")
    qbo_last_updated = parse_qbo_timestamp(
        (qbo_inv.get("MetaData") or {}).get("LastUpdatedTime")
    )

    cur = conn.cursor()
    cur.execute(
        "SELECT memo, statement_memo FROM billing.invoices WHERE qbo_invoice_id = %s",
        (qbo_invoice_id,),
    )
    existing = cur.fetchone()
    was_new = existing is None
    prev_memo = existing[0] if existing else None

    qbo_private_note = qbo_inv.get("PrivateNote")
    qbo_customer_memo = (qbo_inv.get("CustomerMemo") or {}).get("value")
    qbo_memo = qbo_private_note or qbo_customer_memo

    cur.execute("""
        INSERT INTO billing.invoices (
            qbo_invoice_id, doc_number, qbo_customer_id, customer_name,
            txn_date, due_date, total_amt, subtotal, balance, email_status,
            memo, statement_memo,
            line_items, raw, fetched_at, qbo_last_updated_time,
            sync_state, sync_state_changed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s::jsonb, %s::jsonb, now(), %s,
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
            memo                  = EXCLUDED.memo,
            statement_memo        = EXCLUDED.statement_memo,
            line_items            = EXCLUDED.line_items,
            raw                   = EXCLUDED.raw,
            fetched_at            = now(),
            qbo_last_updated_time = EXCLUDED.qbo_last_updated_time,
            sync_state            = 'synced',
            sync_state_changed_at = now(),
            sync_error            = NULL
        WHERE billing.invoices.qbo_last_updated_time IS NULL
           OR EXCLUDED.qbo_last_updated_time IS NULL
           OR billing.invoices.qbo_last_updated_time < EXCLUDED.qbo_last_updated_time
        RETURNING qbo_invoice_id
    """, (
        qbo_invoice_id, qbo_inv.get("DocNumber"),
        customer_ref.get("value"), customer_ref.get("name"),
        qbo_inv.get("TxnDate"), qbo_inv.get("DueDate"),
        float(qbo_inv.get("TotalAmt", 0) or 0),
        qbo_invoice_subtotal(qbo_inv),
        float(qbo_inv.get("Balance", 0) or 0),
        qbo_inv.get("EmailStatus"),
        qbo_memo, qbo_customer_memo or qbo_memo,
        _dumps(line_items), _dumps(qbo_inv),
        qbo_last_updated,
    ))
    did_write = cur.fetchone() is not None
    cur.close()
    return was_new, qbo_invoice_id, prev_memo, qbo_memo, did_write


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
    """Common handler for both Void (kind=voided) and hard-Delete (kind=deleted)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if kind == "voided":
        reason = "invoice_voided"
        sync_err = "voided in QBO"
    else:
        reason = "invoice_deleted_in_qbo"
        sync_err = "deleted in QBO"

    cur.execute("""
        UPDATE public.work_orders
        SET qbo_invoice_id = NULL
        WHERE qbo_invoice_id = %s
        RETURNING wo_number
    """, (qbo_invoice_id,))
    unlinked_wos = [r["wo_number"] for r in cur.fetchall()]

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


def main(qbo_invoice_id: str, operation: str = "", qbo_body: dict | None = None):
    """
    Args:
      qbo_invoice_id: Required. QBO Id of the invoice to refresh.
      operation:      Optional. Webhook operation hint
                      ("Create" | "Update" | "Delete" | "Void" | "Emailed").
                      When provided by the webhook handler, drives the
                      void/delete branch directly. Without it, we detect
                      void heuristically.
      qbo_body:       Optional. Pre-fetched QBO Invoice body (e.g. from CDC).
                      When provided, skips the QBO GET — caller already has
                      authoritative data. Used by cdc_reconciler to avoid a
                      redundant fetch per drifted invoice.
    """
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}

    op = (operation or "").lower()
    print(f"=== refresh_invoice {qbo_invoice_id} (op={op or 'manual'}, "
          f"body_provided={qbo_body is not None}) ===")

    qbo_inv = qbo_body
    if qbo_inv is None:
        access_token, realm_id = refresh_qbo_token()
        resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)

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

    is_voided = (op == "void") or looks_voided(qbo_inv)

    if is_voided:
        conn = get_db_conn()
        try:
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

    conn = get_db_conn()
    try:
        was_new, qbo_invoice_id, prev_memo, qbo_memo, did_write = upsert_invoice(conn, qbo_inv)
        conn.commit()

        # Memo-edit detection only runs if our upsert actually wrote. If OCC
        # blocked us (someone newer landed first), skip — prev_memo we read
        # is stale relative to current cache state, so the comparison would
        # be wrong.
        if did_write and (not was_new) and prev_memo is not None and qbo_memo is not None \
                and prev_memo != qbo_memo:
            cur = conn.cursor()
            cur.execute("""
                UPDATE billing.invoices
                SET memo_locked = true,
                    needs_review_reason = NULLIF(
                        regexp_replace(
                            COALESCE(needs_review_reason, ''),
                            'memo_low_confidence \\([0-9]+%\\)(, )?',
                            '',
                            'g'
                        ),
                        ''
                    )
                WHERE qbo_invoice_id = %s
            """, (qbo_invoice_id,))
            cur.close()
            conn.commit()
            print(f"  external memo edit detected — locked + cleared low-conf flag "
                  f"(prev='{prev_memo[:60] if prev_memo else None}', "
                  f"new='{qbo_memo[:60] if qbo_memo else None}')")

        # WO link is idempotent — safe to run regardless of did_write.
        link_result = link_to_work_order(conn, qbo_invoice_id, qbo_inv.get("DocNumber"))
        conn.commit()

        seeded = 0
        if was_new and link_result.get("linked"):
            seeded = seed_awaiting_pre_processing(conn, qbo_invoice_id)
            conn.commit()

        if was_new and did_write:
            print(f"  new invoice — link={link_result.get('linked')} seeded={seeded}")
        elif link_result.get("linked"):
            print(f"  existing invoice — backfilled WO link to {link_result.get('wo_numbers')}")
        elif not did_write:
            print(f"  upsert no-op (OCC blocked — newer state already in cache)")

        # recheck_invoice_status reads current cache, recomputes — idempotent.
        # Skip when we just inserted+linked: pre_process_invoice will fire via
        # trigger and set the right billing_status.
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
            "did_write": did_write,
            "invoice": _json_safe(dict(invoice_row) if invoice_row else None),
            "link_result": link_result,
            "seeded_awaiting_pre_processing": seeded if was_new else None,
            "recheck": _json_safe(recheck) if recheck else None,
        }
    finally:
        conn.close()
