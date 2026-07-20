# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/drain_qbo_inbox — the QBO sync drainer (ADR 008 §1/§3).
#
# Lives HERE (not f/qbo) deliberately: Windmill's bundler hands scripts in
# the f/qbo folder an EMPTY module for f.billing._lib.qbo (folder-name /
# module-segment collision, proven by probe 2026-07-14) — and the four
# per-entity handlers are f/service_billing scripts anyway.
#
# ONE inbox per system, entity_type as a column: this single worker drains
# billing.qbo_inbox and dispatches each unit to the per-entity refresh
# handler — branches of one workflow ("reflect this entity into the cache"),
# not a router across workflows. Fed by the webhook route (persist envelope,
# return 200), the reconciler probe, sweeps, and manual enqueues; woken by
# trg_wake_qbo_inbox; a 15-min heartbeat is the at-most-once backstop
# [pending schedule creation].
#
# Supersession (ADR 008 §4): a unit whose cache is already fresher than the
# signal is moot — finished at zero API cost (implemented for Invoice, the
# volume entity; others always process).
#
# [pending] the four refresh handlers still self-manage token/conn per call
# (status-quo parity with the old per-event fan-out — strictly better since
# serialized). Their collapse onto f/billing/_lib is the next pass; then this
# drainer refreshes ONCE per drain.

import psycopg2.extras

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import set_rate_limiter

import f.service_billing.refresh_invoice as refresh_invoice
import f.service_billing.refresh_payment as refresh_payment
import f.service_billing.refresh_credit_memo as refresh_credit_memo
import f.service_billing.refresh_customer as refresh_customer

PER_RUN_LIMIT = 100

CLAIM = """
UPDATE billing.qbo_inbox
SET started_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM billing.qbo_inbox
            WHERE finished_at IS NULL AND attempts < 3
            ORDER BY priority, received_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, entity_type, entity_id, operation, received_at
"""

# Supersession probes: cache row already fresher than the signal -> the
# signal is moot (usually OUR OWN write's webhook — the write-time echo
# committed fetched_at before QBO even fired the event). All four mirrored
# entities now carry fetched_at (Customers gained it 2026-07-20), so every
# QBO entity supersedes uniformly.
SUPERSEDED = {
    "Invoice": """SELECT 1 FROM billing.invoices
                  WHERE qbo_invoice_id = %s AND fetched_at > %s""",
    "Payment": """SELECT 1 FROM billing.customer_payments
                  WHERE qbo_payment_id = %s AND fetched_at > %s""",
    "CreditMemo": """SELECT 1 FROM billing.customer_payments
                     WHERE qbo_payment_id = 'CM-' || %s AND fetched_at > %s""",
    "Customer": """SELECT 1 FROM public."Customers"
                   WHERE qbo_customer_id = %s AND fetched_at > %s""",
}


def _row(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def reflect(unit):
    """Handler dispatch — the one place entity_type routes. Each handler is
    the single writer for its cache table (ADR 008 §2)."""
    et, eid, op = unit["entity_type"], unit["entity_id"], unit.get("operation") or ""
    if et == "Invoice":
        return refresh_invoice.main(qbo_invoice_id=eid, operation=op)
    if et == "Payment":
        return refresh_payment.main(qbo_payment_id=eid)
    if et == "CreditMemo":
        return refresh_credit_memo.main(qbo_credit_memo_id=eid, operation=op)
    if et == "Customer":
        return refresh_customer.main(qbo_customer_id=eid)
    return {"status": "skipped", "reason": f"no handler for entity_type {et}"}


def main(max_units: int = 100):
    """Drain the QBO inbox until empty (or the per-run cap)."""
    max_units = max_units or PER_RUN_LIMIT  # Windmill passes null for unset args
    conn = get_db_conn()
    set_rate_limiter(conn)  # ADR 008 §4: every QBO call claims
    try:
        stats, results = {}, []
        for _ in range(max_units):
            unit = _row(conn, CLAIM, ())
            conn.commit()
            if not unit:
                break  # inbox empty

            # supersession: cache already fresher than the signal -> moot
            sup_sql = SUPERSEDED.get(unit["entity_type"])
            if sup_sql and _row(conn, sup_sql,
                                (unit["entity_id"], unit["received_at"])):
                _exec(conn, "UPDATE billing.qbo_inbox "
                            "SET finished_at = now(), error = NULL WHERE id = %s",
                      (unit["id"],))
                stats["superseded"] = stats.get("superseded", 0) + 1
                continue

            try:
                res = reflect(unit) or {}
                _exec(conn, "UPDATE billing.qbo_inbox "
                            "SET finished_at = now(), error = NULL WHERE id = %s",
                      (unit["id"],))
                status = res.get("status", "ok") if isinstance(res, dict) else "ok"
            except Exception as e:
                conn.rollback()
                status = "error"
                # stays open: re-claims until attempts >= 3, then dead-letters
                _exec(conn, "UPDATE billing.qbo_inbox "
                            "SET started_at = NULL, error = %s WHERE id = %s",
                      (f"{type(e).__name__}: {str(e)[:250]}", unit["id"]))
            stats[status] = stats.get(status, 0) + 1
            if len(results) < 25:
                results.append({"entity": f"{unit['entity_type']}:{unit['entity_id']}",
                                "outcome": status})
            print(f"  {unit['entity_type']}:{unit['entity_id']} -> {status}")

        return {"status": "ok", "drained": sum(stats.values()), "stats": stats,
                "results": results}
    finally:
        conn.close()
