# requirements:
# psycopg2-binary

"""
f/billing_audit/build_task_billing_periods

Populate billing_audit.task_billing_periods (the write-ahead invoice promises),
one row per (task, billing_month), from the now-clean maintenance.visits.

Per task-month it accrues:
  visit_count            all visits with task_id in the month
  billable_visit_count   visits with price > 0
  expected_labor_cents   flat_rate_monthly task -> the task's flat monthly amount;
                         per_visit task -> per_visit_rate_cents * billable_visit_count
                         (the POOL MAINTENANCE labor rate x billed visits -- NOT
                         SUM(visit price_cents); a visit's price_cents is the FULL
                         visit charge incl. chemicals/repairs, so summing it would
                         overstate LABOR ~67% and double-count consumables, which
                         we reconcile separately by quantity)
  consumables            {item_name: total_quantity} from consumables_usage
  qbo_customer_id / service_location_id / billing_method / rates  (task terms)
  status = 'visits_accruing'  (invoice match + reconcile come later)

Idempotent UPSERT on (task_id, billing_month): re-running refreshes the accrual
fields but never clobbers qbo_invoice_id / status / labor_ok / consumables_ok /
reconciled_at (set by the later invoice-match + reconcile step).

SAFETY: dry_run=True default -> upsert in a transaction, gather the summary, then
ROLLBACK. Set dry_run=False to commit.
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
  SELECT v.task_id, date_trunc('month', v.visit_date)::date AS billing_month,
         count(*) AS visit_count,
         count(*) FILTER (WHERE COALESCE(v.price_cents,0) > 0) AS billable_visit_count,
         COALESCE(sum(v.price_cents), 0) AS sum_price_cents
  FROM maintenance.visits v
  WHERE v.task_id IS NOT NULL AND v.visit_date IS NOT NULL
  GROUP BY v.task_id, date_trunc('month', v.visit_date)
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
            ELSE COALESCE(tt.per_visit_rate_cents, 0) * vis.billable_visit_count END AS expected_labor_cents,
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
