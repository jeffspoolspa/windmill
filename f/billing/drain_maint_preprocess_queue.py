# requirements:
# psycopg2-binary
# wmill

# f/billing/drain_maint_preprocess_queue
#
# Serially drain billing_audit.maint_preprocess_queue: run maintenance
# preprocessing (credits + status projection) one customer-month at a time.
#
# Module: docs/flows/monthly-maintenance-billing/index.md
# Status: [active]
# Concurrency key: qbo_writer (via the preprocess it calls; schedule also sets
#   concurrent_limit 1 so two drains never overlap)
#
# Triggered by:
#   - schedule (every 2 minutes)
#
# Tables touched:
#   billing_audit.maint_preprocess_queue  [r/w]  claim, stamp finished/error
#   billing_audit.task_billing_periods    [read] self-heal scan (linked but
#                                                never enqueued/preprocessed)
#   (preprocess + projection do the period writes)
#
# External APIs:
#   - QBO via f.billing.preprocess_maint_customer_month (in-process call)
#
# Why this exists:
#   Month-end is a ~520-invoice burst: the link trigger enqueues every
#   customer-month within minutes. Fanning preprocessing out would hammer QBO
#   and race credit applications, so a queue drained one at a time is the
#   deliberate shape (same lesson as the WO pipeline, where pg_net fan-out
#   dropped ~6% of dispatches and an outbox sweep became the primary). The
#   self-heal enqueue also makes the queue eventually consistent if a link
#   trigger insert is ever lost.

import psycopg2
import wmill
from f.billing.preprocess_maint_customer_month import main as preprocess

SUPABASE_RESOURCE = "u/carter/supabase"
MAX_PER_TICK = 10
MAX_ATTEMPTS = 3


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def main(max_per_tick: int = MAX_PER_TICK, dry_run: bool = False):
    conn = get_db_conn()
    results = []
    try:
        # Self-heal: any linked, unlocked, unpreprocessed period with no live
        # queue entry gets one (covers a lost trigger insert).
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO billing_audit.maint_preprocess_queue (qbo_customer_id, billing_month)
               SELECT DISTINCT tbp.qbo_customer_id, tbp.billing_month
               FROM billing_audit.task_billing_periods tbp
               WHERE tbp.qbo_invoice_id IS NOT NULL
                 AND tbp.pre_processed_at IS NULL
                 AND tbp.locked_at IS NULL
                 AND tbp.processing_status NOT IN ('processed')
               ON CONFLICT (qbo_customer_id, billing_month) WHERE finished_at IS NULL
               DO NOTHING"""
        )
        conn.commit()

        for _ in range(max_per_tick):
            # Claim the oldest live entry (SKIP LOCKED: a concurrent manual
            # drain can't double-claim).
            cur.execute(
                """UPDATE billing_audit.maint_preprocess_queue q
                   SET started_at = now(), attempts = attempts + 1
                   WHERE q.id = (
                     SELECT id FROM billing_audit.maint_preprocess_queue
                     WHERE finished_at IS NULL AND attempts < %s
                     ORDER BY enqueued_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1)
                   RETURNING q.id, q.qbo_customer_id, q.billing_month""",
                (MAX_ATTEMPTS,),
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
                break
            qid, customer, month = row
            try:
                out = preprocess(customer, str(month), dry_run=dry_run)
                cur.execute(
                    """UPDATE billing_audit.maint_preprocess_queue
                       SET finished_at = now(), error = NULL WHERE id = %s""",
                    (qid,),
                )
                conn.commit()
                results.append({"customer": customer, "month": str(month), "ok": True,
                                "result": out})
            except Exception as e:
                # leave unfinished -> retried next tick until MAX_ATTEMPTS
                cur.execute(
                    """UPDATE billing_audit.maint_preprocess_queue
                       SET error = %s WHERE id = %s""",
                    (str(e)[:1000], qid),
                )
                conn.commit()
                results.append({"customer": customer, "month": str(month), "ok": False,
                                "error": str(e)[:300]})

        cur.execute(
            """SELECT count(*) FILTER (WHERE finished_at IS NULL AND attempts < %s),
                      count(*) FILTER (WHERE finished_at IS NULL AND attempts >= %s)
               FROM billing_audit.maint_preprocess_queue""",
            (MAX_ATTEMPTS, MAX_ATTEMPTS),
        )
        remaining, dead = cur.fetchone()
        return {"processed": len(results), "remaining": remaining,
                "dead_lettered": dead, "results": results}
    finally:
        conn.close()
