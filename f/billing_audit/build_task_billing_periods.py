# requirements:
# psycopg2-binary

"""
f/billing_audit/build_task_billing_periods

Populate billing_audit.task_billing_periods (the write-ahead invoice promises).
ION task = source of truth: ONE promise per (task, billing_month), and that promise
should match ONE QBO invoice. The promise set is TASK-DRIVEN, not visit-driven:
every (task,month) with visits PLUS every active flat task in each effective month
(a flat task bills regardless of visits -- see task_month CTE).

Per task-month it accrues:
  visit_count            distinct service DAYS attributed to the task that month
  billable_visit_count   distinct service DAYS with a billable log (serviceable,
                         priced > 0, non-QC) -- multiple logs/pools on one day
                         collapse to one day; non-serviceable/skip days excluded
  expected_labor_cents   flat_rate_monthly task -> the task's flat monthly amount
                         (independent of visit count);
                         per_visit / one-time task -> SUM over distinct days of the
                         day's price (MAX billable price that day). Each day's own
                         price IS its labor charge; equals rate x days for a uniform
                         contracted rate, and correctly prices mixed-service months.
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
WITH task_loc AS (
  -- A task's customer = the CANONICAL ion.recurring_tasks.qbo_customer_id (keyed by the
  -- task's ion_task_id, resolved by ION identity / the invoiced customer) -- AUTHORITATIVE
  -- over the address-resolved sl->account, which mis-links duplicate/wrong customers
  -- sharing an address (STEMPF<->HEATON, FLEXER<->DEZEREAUX, etc.). Fall back to
  -- sl->account only when the task has no canonical row.
  SELECT t.id AS task_id, t.service_location_id,
         COALESCE(
           (SELECT rt.qbo_customer_id
            FROM maintenance.task_schedules ts
            JOIN ion.recurring_tasks rt ON rt.ion_task_id = ts.ion_task_id
            WHERE ts.task_id = t.id AND rt.qbo_customer_id IS NOT NULL
            ORDER BY ts.active DESC, ts.updated_at DESC LIMIT 1),
           c.qbo_customer_id
         ) AS qbo_customer_id
  FROM maintenance.tasks t
  JOIN public.service_locations sl ON sl.id = t.service_location_id
  JOIN public."Customers" c ON c.id = sl.account_id
),
day_price AS (
  -- Collapse to ONE billable charge per (task, DAY): multiple ION logs on the same
  -- task-day (duplicate logs, or a QC log alongside the route log) become a single
  -- day. day_price = the day's representative billable price = MAX(price) among that
  -- day's billable logs. Billable excludes:
  --   - non-serviceable logs (holiday/no-access/skip) -- closes the "+1" over-count
  --   - $0 courtesy logs (price 0)
  --   - QUALITY CONTROL visits -- Carter: QC is NON-BILLABLE labor (consumables still
  --     bill, but the QC visit itself is not labor revenue)
  -- day_price is NULL on a day with no billable log (e.g. a skipped/QC-only day).
  SELECT v.task_id, date_trunc('month', v.scheduled_date)::date AS billing_month,
         v.scheduled_date,
         MAX(v.price_cents) FILTER (
           WHERE v.is_serviceable AND COALESCE(v.price_cents,0) > 0
             AND COALESCE(v.service_type,'') NOT ILIKE '%QUALITY CONTROL%') AS day_price
  FROM maintenance.visits v
  WHERE v.task_id IS NOT NULL AND v.scheduled_date IS NOT NULL
  GROUP BY v.task_id, date_trunc('month', v.scheduled_date), v.scheduled_date
),
vis AS (
  -- Roll the per-day charges up to (task, month).
  --   visit_count           = distinct service DAYS (any log)
  --   billable_visit_count  = distinct service DAYS with a billable log
  --   sum_price_cents       = SUM of the per-DAY prices. This is the expected labor
  --     for per_visit tasks: each day's OWN price, NOT rate x count. That matters
  --     when a month mixes service prices on different days -- e.g. COOK has 4 days
  --     of POOL MAINTENANCE 60 ($60) + 1 day of CHEMICAL TESTING ($30); QBO bills
  --     4x$60 + 1x$30 = $270, whereas rate($60) x 5 days = $300 over-bills the chem
  --     day. For a uniform contracted rate this equals rate x billable days, so it
  --     subsumes the old per_visit_rate x count model.
  SELECT task_id, billing_month,
         count(*)                                  AS visit_count,
         count(*) FILTER (WHERE day_price IS NOT NULL) AS billable_visit_count,
         COALESCE(sum(day_price), 0)               AS sum_price_cents
  FROM day_price
  GROUP BY task_id, billing_month
),
months AS (
  -- the set of billing months we have ingested visits for (the spine for flat tasks)
  SELECT DISTINCT date_trunc('month', scheduled_date)::date AS billing_month
  FROM maintenance.visits WHERE scheduled_date IS NOT NULL
),
task_month AS (
  -- TASK-DRIVEN promise set (ION task = source of truth; one promise per task-month):
  --   (a) every (task, month) that has visits -- per_visit + flat alike
  --   (b) every ACTIVE flat-rate task, for each month it is EFFECTIVE, EVEN WITH NO
  --       VISITS. A flat task bills its flat amount regardless of visit count, so a
  --       flat task whose visits landed on a sibling task (e.g. Turners Cove's
  --       FOUNTAIN CLEAN logs mis-attributed to the main FLAT RATE task) must still
  --       produce its own promise. Without (b) the visit-driven build silently drops
  --       the flat task and its invoice goes unmatched. Effective months read off the
  --       task's own billing_method + starts_on/ends_on (terms live on the task now).
  SELECT task_id, billing_month FROM vis
  UNION
  SELECT t.id AS task_id, m.billing_month
  FROM maintenance.tasks t
  JOIN months m ON TRUE
  WHERE t.status = 'active'
    AND t.billing_method = 'flat_rate_monthly'
    AND t.starts_on <= (m.billing_month + interval '1 month - 1 day')::date
    AND (t.ends_on IS NULL OR t.ends_on >= m.billing_month)
),
gov AS (
  -- Financial terms live on the TASK now (one ION contract = one rate), so the per-(task,
  -- month) terms are read straight off the task -- no more picking a "governing" schedule
  -- across versioned / future-dated / rate-less slot rows (the old hack that could flip a
  -- flat community to per_visit, e.g. LOST PLANTATION's $1900 flat vs $95 x 23 days). A
  -- rate change in ION mints a new ion_task_id == a new task, so one rate per task holds.
  -- billing_method NULL -> treated as per_visit (the default).
  SELECT vm.task_id, vm.billing_month,
         COALESCE(t.billing_method, 'per_visit') AS billing_method,
         t.price_per_visit_cents   AS per_visit_rate_cents,
         t.flat_rate_monthly_cents AS flat_rate_monthly_cents
  FROM task_month vm
  JOIN maintenance.tasks t ON t.id = vm.task_id
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
SELECT tm.task_id, tm.billing_month, tl.qbo_customer_id, tl.service_location_id, g.billing_method,
       g.per_visit_rate_cents, g.flat_rate_monthly_cents,
       COALESCE(vis.visit_count, 0), COALESCE(vis.billable_visit_count, 0),
       CASE WHEN g.billing_method = 'flat_rate_monthly'
            -- FLAT task: bills its flat amount regardless of how many (or zero) visits
            -- are attributed to it this month.
            THEN COALESCE(g.flat_rate_monthly_cents, 0)
            -- Everything else (per_visit contracted, one-time, GREEN POOL, ONE TIME
            -- CLEAN, captured closed jobs): SUM of the per-DAY billable prices. Each
            -- day's own price IS its labor charge; for a uniform contracted rate this
            -- equals rate x billable days, and it correctly prices mixed-service
            -- months (e.g. a CHEMICAL TESTING day priced below the maintenance rate).
            ELSE COALESCE(vis.sum_price_cents, 0) END AS expected_labor_cents,
       cons.consumables, 'visits_accruing'
FROM task_month tm
JOIN task_loc tl ON tl.task_id = tm.task_id
LEFT JOIN gov g  ON g.task_id   = tm.task_id AND g.billing_month   = tm.billing_month
LEFT JOIN vis    ON vis.task_id = tm.task_id AND vis.billing_month = tm.billing_month
LEFT JOIN cons   ON cons.task_id = tm.task_id AND cons.billing_month = tm.billing_month
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

# Delete promises that are NOT in the current task-driven set (mirror of task_month):
# a promise is legitimate iff it is (a) backed by visits that month OR (b) an ACTIVE
# flat task effective that month. Anything else is stale -- it survives from a prior
# ingest where that task_id carried visits but a re-ingest re-keyed the task (ION
# EventID/task split). Such orphans keep their old expected_labor and pollute reconcile.
# (NOTE: must mirror the task_month UNION above, else this would delete the flat-task
# promises the build just created.)
DELETE_ORPHANS = """
DELETE FROM billing_audit.task_billing_periods tbp
WHERE NOT EXISTS (
  SELECT 1 FROM maintenance.visits v
  WHERE v.task_id = tbp.task_id
    AND date_trunc('month', v.scheduled_date)::date = tbp.billing_month
)
AND NOT EXISTS (
  SELECT 1 FROM maintenance.tasks t
  WHERE t.id = tbp.task_id AND t.status = 'active'
    AND t.billing_method = 'flat_rate_monthly'
    AND t.starts_on <= (tbp.billing_month + interval '1 month - 1 day')::date
    AND (t.ends_on IS NULL OR t.ends_on >= tbp.billing_month)
)
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
            cur.execute(DELETE_ORPHANS)
            orphans_deleted = cur.rowcount
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
                "rows_upserted": upserted, "orphans_deleted": orphans_deleted,
                "total_rows_after": total, "by_month": by_month}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
