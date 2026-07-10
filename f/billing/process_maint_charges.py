# requirements:
# psycopg2-binary
# requests
# wmill

# f/billing/process_maint_charges — charge-stage worker + sentence handler
# (WORKFLOW_EXECUTION.md; replaces the batch engine process_maint_period).
#
# Unit of work = one customer-month. trg_enqueue_maint_charge fills
# billing_audit.maint_charge_queue as periods reach ready_to_process; this
# worker claims one unit at a time (SKIP LOCKED, priority order) and drains
# until the queue is empty. The handler resolves everything at CLAIM time,
# builds a charge intent, and calls the shared services:
#   not on autopay -> send each invoice   (delivery fact: mark_emailed echo)
#   on autopay     -> charge_and_record(lines=invoice_ids)  (ONE charge for
#                     the summed fresh balances, ONE payment across them,
#                     receipt; WAL + idempotent resume live in the service)
# It stamps no status: processed / needs_review derive via the projection.
#
# Money movement stays human-kicked: no schedule; the UI's Process button
# (or a manual run) starts a drain. Killing it mid-run loses nothing.
#
# Concurrency key: qbo_writer (limit 1) — the write serializer.

import psycopg2.extras
from datetime import datetime

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import refresh_qbo_token, send_invoice
from f.billing._lib.payments import charge_and_record
from f.billing._lib.cache import mark_emailed

STAGE = "maint"

CLAIM = """
UPDATE billing_audit.maint_charge_queue
SET started_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM billing_audit.maint_charge_queue
            WHERE finished_at IS NULL AND attempts < 3
            ORDER BY priority, received_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, qbo_customer_id, billing_month
"""

# Claim-time truth (envelope, not payload): the unit's ready member invoices
# + the customer's route (roster, live PM, fallback default PM, email).
RESOLVE = """
SELECT tbp.qbo_invoice_id, tbp.autopay_customer_id,
       i.doc_number, i.balance, i.email_status,
       c.display_name AS customer_name,
       coalesce(ac.email, c.email) AS email,
       ac.id AS autopay_id,
       pm.id AS cpm_id, pm.qbo_payment_method_id, pm.type AS pm_type,
       (pm.id IS NOT NULL AND pm.is_active AND pm.auto_disabled_at IS NULL
        AND pm.deactivated_at IS NULL) AS pm_live,
       dpm.id AS dpm_id, dpm.qbo_payment_method_id AS dpm_qbo_id,
       dpm.type AS dpm_type
FROM billing_audit.task_billing_periods tbp
LEFT JOIN public."Customers" c ON c.qbo_customer_id = tbp.qbo_customer_id
JOIN billing.invoices i ON i.qbo_invoice_id = tbp.qbo_invoice_id
LEFT JOIN billing.autopay_customers ac ON ac.id = tbp.autopay_customer_id AND ac.is_active
LEFT JOIN billing.customer_payment_methods pm ON pm.id = ac.payment_method_id
LEFT JOIN LATERAL (
  SELECT pm2.id, pm2.qbo_payment_method_id, pm2.type
  FROM billing.customer_payment_methods pm2
  WHERE pm2.qbo_customer_id = tbp.qbo_customer_id
    AND pm2.is_active AND pm2.auto_disabled_at IS NULL AND pm2.deactivated_at IS NULL
  ORDER BY pm2.is_default DESC, pm2.fetched_at DESC
  LIMIT 1
) dpm ON true
WHERE tbp.qbo_customer_id = %s AND tbp.billing_month = %s
  AND tbp.processing_status = 'ready_to_process'
  AND tbp.locked_at IS NULL
ORDER BY i.doc_number
"""


def _rows(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def resolve(conn, cust, month, dry_run):
    """One dict per unit: members + route, resolved NOW (never enqueue-time).
    Falls back to the customer's live default PM when the roster's died,
    re-pointing the roster so the switch is durable and visible."""
    members = _rows(conn, RESOLVE, (cust, month))
    if not members:
        return None
    r = members[0]
    on_autopay = r["autopay_id"] is not None
    pm_id, cpm_id, pm_type, switched = (
        r["qbo_payment_method_id"], r["cpm_id"], r["pm_type"], False)
    if on_autopay and not r["pm_live"] and r["dpm_id"] is not None:
        pm_id, cpm_id, pm_type, switched = (
            r["dpm_qbo_id"], r["dpm_id"], r["dpm_type"], True)
        if not dry_run:
            _exec(conn, "SELECT public.maint_billing_autopay_set_pm(%s, %s)",
                  (cust, cpm_id))
    return {
        "members": members,
        "customer_name": r["customer_name"] or "",
        "email": r["email"],
        "on_autopay": on_autopay,
        "pm_id": pm_id if (r["pm_live"] or switched) else None,
        "cpm_id": str(cpm_id) if cpm_id and (r["pm_live"] or switched) else None,
        "is_ach": "ach" in (pm_type or "").lower() or "bank" in (pm_type or "").lower(),
        "pm_switched": switched,
    }


def build_intent(u, cust, month):
    """Maintenance policy as data: which invoices, whose card, where the
    receipt goes. No amount — the service reads every balance fresh."""
    docs = ", ".join(m["doc_number"] or "?" for m in u["members"])
    month_label = datetime.strptime(month[:7], "%Y-%m").strftime("%B")
    return {
        "stage": STAGE,
        "qbo_invoice_id": u["members"][0]["qbo_invoice_id"],
        "lines": [m["qbo_invoice_id"] for m in u["members"]],
        "payment_method_id": u["pm_id"],
        "cpm_id": u["cpm_id"],
        "channel": "ach" if u["is_ach"] else "card",
        "customer_id": cust,
        "customer_name": u["customer_name"],
        "invoice_number": u["members"][0]["doc_number"],
        "charge_label": docs,
        "payment_ref": docs,
        "memo_prefix": f"{month_label} Pool Maintenance | Inv# {docs}",
        "receipt_email": u["email"],
    }


def deliver(conn, member, email, at, rid):
    """Invoice copy to the customer — NEVER a resend (manual 'Send invoice
    copies' is the only resend path). Success writes the emailed fact."""
    if member["email_status"] == "EmailSent":
        return {"ok": True, "already": True}
    if not email:
        return {"ok": False, "error": "no email on file"}
    r = send_invoice(member["qbo_invoice_id"], email, at, rid)
    if r["ok"]:
        mark_emailed(conn, member["qbo_invoice_id"])
    return r


def process(conn, cust, month, at, rid, dry_run):
    """The sentence: resolve -> build intent -> send or charge -> deliver."""
    u = resolve(conn, cust, month, dry_run)
    if not u:
        return {"status": "nothing_ready"}  # projection moved on; unit is moot
    docs = ", ".join(m["doc_number"] or "?" for m in u["members"])

    if not u["on_autopay"] or not u["pm_id"]:
        if dry_run:
            return {"status": "dry_run", "customer": u["customer_name"],
                    "periods": len(u["members"]),
                    "plan": f"send invoice(s) #{docs} to "
                            f"{u['email']} (no autopay / no live payment method)"}
        sent = [deliver(conn, m, u["email"], at, rid) for m in u["members"]]
        _project(conn, month, cust)
        ok = all(s.get("ok") for s in sent)
        return {"status": "invoices_sent" if ok else "email_failed",
                "sent": sent,
                "errors": [s.get("error") for s in sent if s.get("error")] or None}

    if dry_run:
        total = sum(float(m["balance"] or 0) for m in u["members"])
        return {"status": "dry_run", "customer": u["customer_name"],
                "periods": len(u["members"]),
                "plan": f"charge {'ach' if u['is_ach'] else 'card'} ~{total:.2f} "
                        f"(fresh-read decides) for invoice(s) #{docs}, one payment "
                        f"across all, receipt to {u['email']}"
                        + (" [roster PM dead — would switch to QBO default]"
                           if u["pm_switched"] else "")}

    r = charge_and_record(conn, build_intent(u, cust, month), at, rid)

    # [pending ADR 009 §D] roster health still stamped until v_autopay_health
    # lands and the UI reads it — the one derivable write left in this file.
    if r["status"] == "declined":
        _exec(conn, """UPDATE billing.autopay_customers
                       SET consecutive_declines = consecutive_declines + 1,
                           payment_status = 'payment_issue', updated_at = now()
                       WHERE id = %s""", (u["members"][0]["autopay_customer_id"],))
    elif r["status"] == "succeeded":
        _exec(conn, """UPDATE billing.autopay_customers
                       SET consecutive_declines = 0, payment_status = 'good',
                           updated_at = now()
                       WHERE id = %s""", (u["members"][0]["autopay_customer_id"],))

    if r["status"] in ("succeeded", "declined", "already_paid"):
        # paid copy / pay-it-yourself copy — either way the customer gets it
        for m in u["members"]:
            deliver(conn, m, u["email"], at, rid)
    _project(conn, month, cust)
    return {**{k: r[k] for k in ("status", "amount", "charge_id", "payment_id",
                                 "receipt_sent", "error", "resumed")},
            "invoices": docs,
            "pm_switched_to_qbo_default": u["pm_switched"] or None}


def _project(conn, month, cust):
    # derived state derives: processed / needs_review come from the fact log
    _exec(conn, "SELECT billing_audit.project_maint_processing_status(%s, %s)",
          (month, cust))


# ── the worker ───────────────────────────────────────────────────────────────

def main(billing_month: str = None,
         qbo_customer_ids: list = None,
         dry_run: bool = True,
         max_units: int = 1000):
    """Drain the charge queue until empty (or plan without touching it).

    dry_run=True: NO queue writes, no external calls — resolves the given
      customer-months (or every live queue unit) and returns the plans.
    live + qbo_customer_ids: enqueue those units (coalesced), then drain the
      WHOLE queue. Killing the worker loses nothing: unclaimed rows wait,
      claimed-but-unfinished rows re-claim (3 strikes dead-letters), and the
      service's persisted idempotency keys make re-running any unit safe.
    """
    conn = get_db_conn()
    try:
        month = f"{billing_month}-01" if billing_month and len(billing_month) == 7 \
            else billing_month

        if dry_run:
            if qbo_customer_ids and month:
                units = [(c, month) for c in dict.fromkeys(qbo_customer_ids)]
            else:
                units = [(q["qbo_customer_id"], str(q["billing_month"])) for q in
                         _rows(conn, """SELECT qbo_customer_id, billing_month
                                        FROM billing_audit.maint_charge_queue
                                        WHERE finished_at IS NULL AND attempts < 3
                                        ORDER BY priority, received_at LIMIT %s""",
                               (max_units,))]
            at, rid = refresh_qbo_token()
            results = [{"customer": c, "month": m,
                        **process(conn, c, m, at, rid, True)} for c, m in units]
            by_status = {}
            for r in results:
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            return {"dry_run": True, "units": len(results),
                    "periods": sum(r.get("periods", 0) for r in results),
                    "by_status": by_status, "results": results}

        if qbo_customer_ids and month:
            _exec(conn, """INSERT INTO billing_audit.maint_charge_queue
                             (qbo_customer_id, billing_month)
                           SELECT unnest(%s::text[]), %s::date
                           ON CONFLICT (qbo_customer_id, billing_month)
                             WHERE finished_at IS NULL DO NOTHING""",
                  (list(dict.fromkeys(qbo_customer_ids)), month))

        at, rid = refresh_qbo_token()
        results = []
        for _ in range(max_units):
            claimed = _rows(conn, CLAIM, ())
            conn.commit()
            if not claimed:
                break  # queue empty — drained
            unit = claimed[0]
            cust, m = unit["qbo_customer_id"], str(unit["billing_month"])
            try:
                res = process(conn, cust, m, at, rid, False)
                _exec(conn, """UPDATE billing_audit.maint_charge_queue
                               SET finished_at = now(), error = NULL
                               WHERE id = %s""", (unit["id"],))
            except Exception as e:
                conn.rollback()
                res = {"status": "error", "error": str(e)[:300]}
                # stays open: re-claims until attempts >= 3, then dead-letters
                _exec(conn, """UPDATE billing_audit.maint_charge_queue
                               SET started_at = NULL, error = %s
                               WHERE id = %s""", (str(e)[:300], unit["id"]))
            results.append({"customer": cust, "month": m, **res})
            print(f"  [{len(results)}] {cust} {m} -> {res.get('status')}"
                  + (f" ({res.get('error')})" if res.get("error") else ""))

        by_status = {}
        for r in results:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {"dry_run": False, "drained": len(results),
                "by_status": by_status, "results": results}
    finally:
        conn.close()
