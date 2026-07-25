# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/dispatch_pre_processing — the pre-process queue worker.
#
# WORKFLOW_EXECUTION applied to service-billing enrichment: the WO-link
# trigger (trg_enqueue_service_preprocess) writes the queue and wakes this
# worker; the 60s schedule is the heartbeat + self-heal (pg_net is
# at-most-once — the outbox lesson: ~6% of direct fires dropped under burst
# and invoices stuck invisibly).
#
# Claim, run, finish. Eligibility is a SQL predicate, stated once and used
# three ways: enqueue what's missing, retire what's moot, claim what's live.
# One QboClient for the whole drain — one token refresh, not one per invoice.
#
# Concurrency: concurrent_limit 1.

import time

from f.billing._lib.db import get_db_conn, query_one, execute_sql
from f.billing._lib.clients import QboClient
from f.service_billing.pre_process_invoice import enrich

PER_RUN_LIMIT = 50
GRACE_MINUTES = 2  # let the wake path win before self-heal re-enqueues

# The one rule: is this invoice still worth enriching?
ELIGIBLE = """
  i.billing_status = 'awaiting_pre_processing'
  AND i.pre_processed_at IS NULL
  AND i.subtotal_ok IS TRUE
  AND w.billable IS TRUE AND w.skipped_at IS NULL
"""

# Lost-trigger backstop: an eligible invoice with no live queue row gets one.
SELF_HEAL = f"""
INSERT INTO billing.service_preprocess_queue (qbo_invoice_id)
SELECT i.qbo_invoice_id FROM billing.invoices i
JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
WHERE {ELIGIBLE} AND i.fetched_at < now() - make_interval(mins => %s)
ON CONFLICT (qbo_invoice_id) WHERE finished_at IS NULL DO NOTHING
"""

# Moot rows (gate flipped them, WO unlinked, already enriched) retire in one
# set-based pass, so CLAIM only ever returns live work.
RETIRE = f"""
UPDATE billing.service_preprocess_queue q SET finished_at = now(), error = NULL
WHERE q.finished_at IS NULL AND NOT EXISTS (
  SELECT 1 FROM billing.invoices i
  JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
  WHERE i.qbo_invoice_id = q.qbo_invoice_id AND {ELIGIBLE})
"""

# Eligibility is re-checked here, not just in RETIRE: a drain can run for a
# while, and a work order skipped in the UI mid-drain must not still be
# enriched. Claiming a row is the only moment that decision is safe to make.
CLAIM = f"""
UPDATE billing.service_preprocess_queue
SET started_at = now(), attempts = attempts + 1
WHERE id = (SELECT q.id FROM billing.service_preprocess_queue q
            JOIN billing.invoices i ON i.qbo_invoice_id = q.qbo_invoice_id
            JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
            WHERE q.finished_at IS NULL AND q.attempts < 3
              AND billing.credits_cache_fresh()
              AND {ELIGIBLE}
            ORDER BY q.priority, q.received_at
            FOR UPDATE OF q SKIP LOCKED LIMIT 1)
RETURNING id, qbo_invoice_id
"""

FINISH = ("UPDATE billing.service_preprocess_queue "
          "SET finished_at = now(), error = NULL WHERE id = %s")
# Failed units stay open and re-claim until attempts >= 3, then dead-letter.
RELEASE = ("UPDATE billing.service_preprocess_queue "
           "SET started_at = NULL, error = %s WHERE id = %s")


def main():
    """Self-heal, retire, then drain until empty (or the per-run cap). An
    idle run makes no QBO calls — the client is built on the first real unit."""
    started = time.time()
    conn = get_db_conn()
    try:
        execute_sql(conn, SELF_HEAL, (GRACE_MINUTES,))
        execute_sql(conn, RETIRE, ())

        qbo, done, failed, results = None, 0, 0, []
        for _ in range(PER_RUN_LIMIT):
            unit = query_one(conn, CLAIM, ())
            conn.commit()
            if not unit:
                break
            qbo = qbo or QboClient(conn)   # first real unit: one token refresh
            try:
                results.append(enrich(conn, qbo, unit["qbo_invoice_id"]))
                execute_sql(conn, FINISH, (unit["id"],))
                done += 1
            except Exception as e:
                conn.rollback()
                error = f"{type(e).__name__}: {str(e)[:200]}"
                execute_sql(conn, RELEASE, (error, unit["id"]))
                results.append({"qbo_invoice_id": unit["qbo_invoice_id"],
                                "error": error})
                failed += 1

        return {"enriched": done, "failed": failed, "results": results[:20],
                "elapsed_s": round(time.time() - started, 1)}
    finally:
        conn.close()
