# requirements:
# psycopg2-binary

"""
f/billing_audit/build_task_billing_periods

Populate billing_audit.task_billing_periods (the write-ahead invoice promises),
one row per (task, billing_month), from the now-clean maintenance.visits.

Per task-month it accrues:
  visit_count            distinct service DAYS with task_id in the month
  billable_visit_count   distinct serviceable, priced>0, NON-QC service days
  expected_labor_cents   flat_rate_monthly -> flat monthly amount;
                         per_visit WITH a contracted rate -> rate x billable days;
                         per_visit NO rate (one-time/lump-sum: GREEN POOL, ONE TIME
                          CLEAN, captured closed jobs) -> SUM of serviceable prices
  consumables            {item_name: total_quantity} from consumables_usage
  status = 'visits_accruing'

KNOWN RESIDUAL: flat tasks with ZERO visits in a month are not emitted (this builder
is visit-driven); a few community accounts have a 2nd flat task with no logs that
still bills -> follow-up (emit active flat tasks regardless of visits).

SAFETY: dry_run=True default -> rolls back. Set dry_run=False to commit.
"""

from f.ION._lib.upsert import _connect

UPSERT = """
WITH task_terms AS (
  SELECT t.id AS task_id, t.service_location_id, c.qbo_customer_id,
         (array_agg(ts.billing_method ORDER BY ts.active DESC, ts.updated_at DESC)
            FILTER (WHERE ts.billing_method IS NOT NULL))[1] AS billing_method,
         max(ts.price_per_visit_cents)   AS per_visit_rate_cents,
         max(ts.flat_rate_monthly_cents) AS flat_rate_monthly_cents
  FROM maintenance.tasks t
  JOIN public.service_locations sl ON sl.id = t.service_location_id
  JOIN public."Customers" c ON c.id = sl.account_id
  LEFT JOIN maintenance.task_schedules ts ON ts.task_id = t.id
  GROUP BY t.id, t.service_location_id, c.qbo_customer_id
),
vis AS (
  -- One billable visit per (task, DAY): multiple ION logs/pools on one day collapse
  -- via COUNT(DISTINCT scheduled_date). Excluded from billable labor: non-serviceable
  -- (holiday/skip), $0 courtesy logs, and QUALITY CONTROL (non-billable labor per
  -- Carter; its consumables still bill). sum_price_cents = the billable logs' own
  -- prices -> expected fallback for one-time/no-rate tasks.
  SELECT v.task_id, date_trunc('month', v.scheduled_date)::date AS billing_month,
         count(DISTINCT v.scheduled_date) AS visit_count,
         count(DISTINCT v.scheduled_date)
           FILTER (WHERE v.is_serviceable AND COALESCE(v.price_cents,0) > 0
                     AND COALESCE(v.service_type,'') NOT ILIKE '%QUALITY CONTROL%')
           AS billable_visit_count,
         COALESCE(sum(v.price_cents) FILTER (
           WHERE v.is_serviceable AND COALESCE(v.price_cents,0) > 0
             AND COALESCE(v.service_type,'') NOT ILIKE '%QUALITY CONTROL%'), 0) AS sum_price_cents
  FROM maintenance.visits v
  WHERE v.task_id IS NOT NULL AND v.scheduled_date IS NOT NULL
  GROUP BY v.task_id, date_trunc('month', v.scheduled_date)
),
cons AS (
  SELECT task_id, billing_month, jsonb_object_agg(item_name, qty) AS consumables
  FROM (
    SELECT v.task_id, date_trunc('month', v.visit_date)::date AS billing_month,
           cu.item_name, sum(cu.quantity) AS qty
    FROM maintenance.visits v
    JOIN maintenance.consumables_usage cu ON cu.visit_id = v.id
    WHERE v.task_id IS NOT NULL AND cu.item_name IS NOT NULL
    GROUP BY 1, 2, cu.item_name
  ) z
  GROUP BY task_id, billing_month
)
INSERT INTO billing_audit.task_billing_periods
  (task_id, billing_month, qbo_customer_id, service_location_id, billing_method,
   per_visit_rate_cents, flat_rate_monthly_cents, visit_count, billable_visit_count,
   expected_labor_cents, consumables, status)
SELECT vis.task_id, vis.billing_month, tt.qbo_customer_id, tt.service_location_id, tt.billing_method,
       tt.per_visit_rate_cents, tt.flat_rate_monthly_cents, vis.visit_count, vis.billable_visit_count,
       CASE WHEN tt.billing_method = 'flat_rate_monthly'
            THEN COALESCE(tt.flat_rate_monthly_cents, 0)
            WHEN tt.per_visit_rate_cents IS NOT NULL
            THEN tt.per_visit_rate_cents * vis.billable_visit_count
            ELSE vis.sum_price_cents END AS expected_labor_cents,
       cons.consumables, 'visits_accruing'
FROM vis
JOIN task_terms tt ON tt.task_id = vis.task_id
LEFT JOIN cons ON cons.task_id = vis.task_id AND cons.billing_month = vis.billing_month
ON CONFLICT (task_id, billing_month) DO UPDATE SET
   qbo_customer_id        = EXCLUDED.qbo_customer_id,
   service_location_id    = EXCLUDED.service_location_id,
   billing_method         = EXCLUDED.billing_method,
   per_visit_rate_cents   = EXCLUDED.per_visit_rate_cents,
   flat_rate_monthly_cents= EXCLUDED.flat_rate_monthly_cents,
   visit_count            = EXCLUDED.visit_count,
   billable_visit_count   = EXCLUDED.billable_visit_count,
   expected_labor_cents   = EXCLUDED.expected_labor_cents,
   consumables            = EXCLUDED.consumables,
   updated_at             = now();
"""

SUMMARY = """
SELECT to_char(billing_month,'YYYY-MM') AS month,
       count(*) AS promises,
       count(DISTINCT qbo_customer_id) AS customers,
       sum(billable_visit_count) AS billable_visits,
       round(sum(expected_labor_cents)/100.0, 2) AS expected_labor_usd,
       count(*) FILTER (WHERE billing_method='flat_rate_monthly') AS flat,
       count(*) FILTER (WHERE billing_method='per_visit') AS per_visit
FROM billing_audit.task_billing_periods
GROUP BY billing_month ORDER BY billing_month;
"""


def main(supabase_connection, dry_run=True):
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute(UPSERT)
            upserted = cur.rowcount
            cur.execute(SUMMARY)
            cols = [d[0] for d in cur.description]
            by_month = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM billing_audit.task_billing_periods")
            total = cur.fetchone()[0]
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {"dry_run": dry_run, "committed": not dry_run,
                "rows_upserted": upserted, "total_rows_after": total, "by_month": by_month}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
