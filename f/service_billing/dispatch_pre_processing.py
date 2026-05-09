# f/service_billing/dispatch_pre_processing
#
# Outbox-pattern worker that replaces the unreliable pg_net trigger as the
# primary mechanism for getting newly-linked invoices through pre_process.
#
# Why this exists:
#   The original design fired pre_process_invoice via pg_net.http_post from
#   a row trigger on work_orders. That's at-most-once delivery — pg_net's
#   queue can drop requests under burst load (observed: ~3 of 50 requests
#   dropped during a bulk QBO sync, leaving 8 service-billing invoices
#   stuck at billing_status='awaiting_pre_processing' with no UI visibility).
#
# What this does:
#   Every 60s, query for invoices that:
#     - have billing_status='awaiting_pre_processing'
#     - have a billable, non-skipped linked work order
#     - have never been pre-processed (pre_processed_at IS NULL)
#     - have been sitting that way for >= 2 minutes (gives pg_net's happy
#       path a chance before we duplicate the work)
#   For each, call f.service_billing.pre_process_invoice.main() in-process.
#   Pre-process is idempotent — re-running on a row that's already been
#   processed would just re-stamp it with the same enrichment.
#
# Why in-process import vs Windmill API call:
#   Each pre_process run takes ~5-10s. Calling via wmill.run_script_by_path
#   adds ~200ms dispatch overhead per call AND respects pre_process_invoice's
#   own concurrent_limit (would queue 2-at-a-time). For drain workloads
#   this batters latency. In-process call goes straight through; the worker
#   is the SOLE concurrent caller (concurrent_limit=1 on this script) so
#   total parallelism is preserved.
#
# Bounds per tick:
#   LIMIT 25 invoices per run. Anything more than that means a major
#   incident — the next tick will pick up the rest.

import time

import psycopg2
import psycopg2.extras
import wmill

import f.service_billing.pre_process_invoice as pre_process_invoice

SUPABASE_RESOURCE = "u/carter/supabase"
PER_TICK_LIMIT = 25
STUCK_GRACE_MINUTES = 2  # let pg_net try first; we're the backstop


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def find_stuck_invoices(conn, limit: int = PER_TICK_LIMIT):
    """Stuck = awaiting_pre_processing AND linked-to-billable-WO AND never
    pre-processed AND subtotal_ok=true AND sitting >= grace window.

    The WO-link filter is essential — without it we'd also try to pre-process
    maintenance autopay invoices (separate pipeline, no WO).

    The subtotal_ok=TRUE gate is the single source of truth for "data is
    self-consistent enough to attempt enrichment." When subtotal_ok is FALSE
    or NULL, the projection trigger has already routed the invoice to
    needs_review (subtotal_mismatch reason). Dispatching pre_process on
    those would just waste a Claude call on data we know is wrong.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT i.qbo_invoice_id,
               i.doc_number,
               i.customer_name,
               i.fetched_at,
               extract(epoch from (now() - i.fetched_at))::int AS age_seconds,
               w.wo_number
          FROM billing.invoices i
          JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
         WHERE i.billing_status = 'awaiting_pre_processing'
           AND w.billable = true
           AND w.skipped_at IS NULL
           AND i.pre_processed_at IS NULL
           AND i.subtotal_ok IS TRUE
           AND i.fetched_at < now() - make_interval(mins => %s)
         ORDER BY i.fetched_at ASC
         LIMIT %s
        """,
        (STUCK_GRACE_MINUTES, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def main():
    """Run the dispatch sweep. Schedule: every 60 seconds.

    Returns a summary dict with how many stuck invoices we found and the
    per-invoice outcome (success / needs_review / error). The Windmill UI
    surfaces these results so cron history is auditable.
    """
    started = time.time()
    conn = get_db_conn()

    try:
        stuck = find_stuck_invoices(conn)
    finally:
        conn.close()

    if not stuck:
        return {
            "status": "ok",
            "stuck_found": 0,
            "elapsed_s": round(time.time() - started, 1),
            "note": "nothing to dispatch",
        }

    print(f"=== dispatch_pre_processing: {len(stuck)} stuck invoice(s) ===")

    results = []
    stats = {
        "ready_to_process": 0,
        "needs_review": 0,
        "error": 0,
        "skipped": 0,
        "success": 0,        # bulk-all returns this
    }

    for entry in stuck:
        qid = entry["qbo_invoice_id"]
        wo  = entry["wo_number"]
        age = entry["age_seconds"]
        try:
            res = pre_process_invoice.main(qbo_invoice_id=qid, force=False)
        except Exception as e:
            res = {"status": "error", "qbo_invoice_id": qid,
                   "error": f"{type(e).__name__}: {str(e)[:200]}"}

        status = res.get("status", "error")
        stats[status] = stats.get(status, 0) + 1
        results.append({
            "qbo_invoice_id":      qid,
            "doc_number":          entry["doc_number"],
            "customer_name":       entry["customer_name"],
            "wo_number":           wo,
            "age_seconds":         age,
            "outcome":             status,
            "needs_review_reason": res.get("needs_review_reason")
                                   or res.get("reason")
                                   or res.get("error"),
        })
        print(f"  {qid}  WO {wo}  age={age}s  -> {status}")

    elapsed = time.time() - started
    print(f"=== done in {elapsed:.1f}s: {stats} ===")

    return {
        "status":      "ok",
        "stuck_found": len(stuck),
        "elapsed_s":   round(elapsed, 1),
        "stats":       stats,
        "results":     results,
    }
