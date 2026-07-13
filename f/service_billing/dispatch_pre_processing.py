# f/service_billing/dispatch_pre_processing — the pre-process queue worker.
#
# WORKFLOW_EXECUTION applied to service-billing enrichment: the WO-link
# trigger (trg_enqueue_service_preprocess) writes billing.service_preprocess_
# queue + wakes this worker; the 60s schedule is the heartbeat + self-heal
# (pg_net is at-most-once — the outbox lesson: ~6% of direct fires dropped
# under burst and invoices stuck invisibly).
#
# Claim one row at a time (SKIP LOCKED, priority order, 3-attempt
# dead-letter), check ELIGIBILITY AT CLAIM TIME (billable WO, not skipped,
# still awaiting, subtotal_ok — enqueue is dumb, the worker decides), then
# run the enrichment sentence IN-PROCESS (one DB connection + ONE token
# refresh per drain, not per invoice — the old per-invoice main() calls
# rotated the QBO refresh token once per unit).
#
# Concurrency: concurrent_limit 1 (sole caller of the enrichment handler;
# per-call QBO volume is governed by the shared rate bucket).

import time

import psycopg2.extras
import wmill

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import set_rate_limiter, refresh_qbo_token
import f.service_billing.pre_process_invoice as pre_process

PER_RUN_LIMIT = 50
GRACE_MINUTES = 2  # let the wake path win before self-heal re-enqueues

CLAIM = """
UPDATE billing.service_preprocess_queue
SET started_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM billing.service_preprocess_queue
            WHERE finished_at IS NULL AND attempts < 3
            ORDER BY priority, received_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, qbo_invoice_id
"""

# Lost-trigger backstop: any eligible invoice with no live queue row gets one.
SELF_HEAL = """
INSERT INTO billing.service_preprocess_queue (qbo_invoice_id)
SELECT i.qbo_invoice_id
FROM billing.invoices i
JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
WHERE i.billing_status = 'awaiting_pre_processing'
  AND w.billable = true AND w.skipped_at IS NULL
  AND i.pre_processed_at IS NULL
  AND i.subtotal_ok IS TRUE
  AND i.fetched_at < now() - make_interval(mins => %s)
ON CONFLICT (qbo_invoice_id) WHERE finished_at IS NULL DO NOTHING
"""

# Claim-time truth: is this unit still worth enriching?
ELIGIBLE = """
SELECT (i.billing_status = 'awaiting_pre_processing'
        AND w.billable IS TRUE AND w.skipped_at IS NULL
        AND i.pre_processed_at IS NULL
        AND i.subtotal_ok IS TRUE) AS ok
FROM billing.invoices i
LEFT JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
WHERE i.qbo_invoice_id = %s
"""


def _row(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def main():
    """Self-heal, then drain the pre-process queue until empty (or the
    per-run cap). Idle runs make no QBO calls (no token rotation)."""
    started = time.time()
    conn = get_db_conn()
    set_rate_limiter(conn)  # ADR 008 §4: every QBO call claims
    try:
        _exec(conn, SELF_HEAL, (GRACE_MINUTES,))

        stats, results, creds = {}, [], None
        for _ in range(PER_RUN_LIMIT):
            unit = _row(conn, CLAIM, ())
            conn.commit()
            if not unit:
                break  # queue empty
            qid = unit["qbo_invoice_id"]

            elig = _row(conn, ELIGIBLE, (qid,))
            if not elig or not elig["ok"]:
                # moot at claim time (gate flipped it, WO unlinked, already
                # enriched) — finish clean; self-heal re-enqueues if it ever
                # becomes eligible again
                _exec(conn, "UPDATE billing.service_preprocess_queue "
                            "SET finished_at = now(), error = NULL WHERE id = %s",
                      (unit["id"],))
                stats["moot"] = stats.get("moot", 0) + 1
                continue

            if creds is None:  # first real unit: ONE refresh per drain
                at, rid = refresh_qbo_token()
                api_key = wmill.get_variable(pre_process.OPENAI_KEY_VAR)
                creds = (at, rid, api_key)

            try:
                res = pre_process.process_one(conn, qid, *creds, force=False)
                _exec(conn, "UPDATE billing.service_preprocess_queue "
                            "SET finished_at = now(), error = NULL WHERE id = %s",
                      (unit["id"],))
            except Exception as e:
                conn.rollback()
                res = {"status": "error", "qbo_invoice_id": qid,
                       "error": f"{type(e).__name__}: {str(e)[:200]}"}
                # stays open: re-claims until attempts >= 3, then dead-letters
                _exec(conn, "UPDATE billing.service_preprocess_queue "
                            "SET started_at = NULL, error = %s WHERE id = %s",
                      (res["error"], unit["id"]))

            status = res.get("status", "error")
            stats[status] = stats.get(status, 0) + 1
            if len(results) < 20:
                results.append({"qbo_invoice_id": qid, "outcome": status,
                                "reason": res.get("needs_review_reason")
                                          or res.get("reason") or res.get("error")})
            print(f"  {qid} -> {status}")

        return {"status": "ok", "drained": sum(stats.values()), "stats": stats,
                "results": results, "elapsed_s": round(time.time() - started, 1)}
    finally:
        conn.close()
