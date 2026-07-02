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
                         (multiple logs/pools on one day collapse to one day)
  billable_visit_count   distinct SERVICEABLE service DAYS (non-serviceable/skip days
                         excluded -- no service happened)
  expected_labor_cents   the TASK dictates rate and billability, not the visit:
                         flat_rate_monthly task -> the task's flat monthly amount
                         (independent of visit count);
                         per_visit / one-time / QC -> task.price_per_visit_cents x
                         billable_visit_count. A QC task carries rate 0, so its days
                         multiply out to $0 labor (consumables still bill).
  consumables            {item_name: total_quantity} from consumables_usage
  expected_consumable_cents  Model B: SUM(item qty x maintenance.consumables.unit_price_cents),
                         priced by ion_item_id via the consumable master (immune to item_id null-out)
  unpriced_consumables   {item_name: qty} for items with no catalog price yet (finite worklist)
  expected_total_cents   GENERATED = expected_labor_cents + expected_consumable_cents
  qbo_customer_id / service_location_id / billing_method / rates  (task terms)
  status = 'visits_accruing'  (invoice match + reconcile come later)

Idempotent UPSERT on (task_id, billing_month): re-running refreshes the accrual
fields (labor, consumables, priced consumable total) but never clobbers qbo_invoice_id /
status / labor_ok / consumables_ok / reconciled_at (set by the later invoice-match +
reconcile step). expected_total_cents is a generated column -- it follows its parts.

LIVE LEDGER: built to run continuously (daily, or any time mid-month). Each run re-UPSERTs
the OPEN months so the table always shows where billing currently stands -- mid-month revenue
= SUM(expected_total_cents) over the current month; high-bill early-warning = a customer whose
current-month expected_total is already high. Finalized months are LOCKED (locked_at) and
skipped by every subsequent run -- no wasted recompute, and a late retroactive visit edit
can't disturb a closed month. Lock via main(lock_through=<date>) after billing + reconcile.

SAFETY: dry_run=True default -> upsert in a transaction, gather the summary, then
ROLLBACK. Set dry_run=False to commit.
"""

from f.ION._lib.upsert import _connect

UPSERT = """
WITH locked_months AS (
  -- Finalized months: skip them entirely (don't recompute, don't delete). A month is locked
  -- once its rows carry locked_at (set by lock_through, after it is billed + reconciled). This
  -- is what lets the builder run daily cheaply -- only the OPEN month(s) are ever rebuilt.
  SELECT DISTINCT billing_month FROM billing_audit.task_billing_periods WHERE locked_at IS NOT NULL
),
task_loc AS (
  -- ADR 007 §9: a task carries customer_id, NOT a location (tasks.service_location_id was
  -- dropped in #30). qbo_customer_id = the CANONICAL ion.recurring_tasks.qbo_customer_id
  -- (keyed by the task's ion_task_id) -- AUTHORITATIVE over address-resolved links that
  -- mis-map duplicate customers sharing an address (STEMPF<->HEATON, ...); fall back to the
  -- task customer's qbo id. service_location = the customer's confirmed PRIMARY location
  -- (public.v_customer_primary_location), the routing/billing location per ADR 007 §9.
  SELECT t.id AS task_id,
         cpl.service_location_id,
         COALESCE(
           (SELECT rt.qbo_customer_id
            FROM maintenance.task_schedules ts
            JOIN ion.recurring_tasks rt ON rt.ion_task_id = ts.ion_task_id
            WHERE ts.task_id = t.id AND rt.qbo_customer_id IS NOT NULL
            ORDER BY ts.active DESC, ts.updated_at DESC LIMIT 1),
           c.qbo_customer_id
         ) AS qbo_customer_id
  FROM maintenance.tasks t
  JOIN public."Customers" c ON c.id = t.customer_id
  LEFT JOIN public.v_customer_primary_location cpl ON cpl.customer_id = t.customer_id
),
days AS (
  -- Collapse to distinct service DAYS per (task, month). Multiple ION logs on one
  -- task-day (a duplicate log, or a QC log beside the route log) collapse to ONE day.
  --   visit_count           = distinct service days (any log)
  --   billable_visit_count  = distinct SERVICEABLE service days (holiday / no-access /
  --                           skip days excluded -- no service happened -> no charge)
  -- Billability + rate are the TASK's job now, NOT the visit's: we no longer look at the
  -- visit's own price or a QUALITY CONTROL service string. A QC contract is its OWN task
  -- carrying price_per_visit_cents = 0 (confirmed in maintenance.tasks), so its days
  -- multiply out to $0 labor in expected_labor_cents below; consumables still bill.
  SELECT v.task_id, date_trunc('month', v.scheduled_date)::date AS billing_month,
         count(DISTINCT v.scheduled_date)                                 AS visit_count,
         count(DISTINCT v.scheduled_date) FILTER (WHERE v.is_serviceable) AS billable_visit_count
  FROM maintenance.visits v
  WHERE v.task_id IS NOT NULL AND v.scheduled_date IS NOT NULL
    AND date_trunc('month', v.scheduled_date)::date NOT IN (SELECT billing_month FROM locked_months)
  GROUP BY v.task_id, date_trunc('month', v.scheduled_date)
),
months AS (
  -- the set of OPEN (unlocked) billing months we have ingested visits for (spine for flat tasks)
  SELECT DISTINCT date_trunc('month', scheduled_date)::date AS billing_month
  FROM maintenance.visits WHERE scheduled_date IS NOT NULL
    AND date_trunc('month', scheduled_date)::date NOT IN (SELECT billing_month FROM locked_months)
),
task_month AS (
  -- TASK-DRIVEN promise set (ION task = source of truth; one promise per task-month):
  --   (a) every (task, month) that has visits -- per_visit + flat alike
  --   (b) every ACTIVE flat-rate task, for each month it is EFFECTIVE, EVEN WITH NO
  --       VISITS. A flat task bills its flat amount regardless of visit count, so a
  --       flat task whose visits landed on a sibling task (e.g. Turners Cove's
  --       FOUNTAIN CLEAN logs mis-attributed to the main FLAT RATE task) must still
  --       produce its own promise. Without (b) the visit-driven build silently drops
  --       the flat task and its invoice goes unmatched.
  SELECT task_id, billing_month FROM days
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
  -- Model B: price each consumable into the expected total via the consumable master
  -- (maintenance.consumables), keyed on ion_item_id (100% present, 1:1 with item_name).
  -- Pricing off ion_item_id -- NOT consumables_usage.item_id -- so it is immune to the
  -- item_id null-out; every distinct item has a catalog row.
  --   consumables               {item_name: total_qty}  (UNCHANGED -- reconcile reads this
  --                             for the per-item under-billed quantity check)
  --   expected_consumable_cents SUM over items of (month qty x consumables.unit_price_cents).
  --                             The catalog price is the BILLED (QBO) price, so the expected
  --                             total reconciles against the ION invoice (= QBO).
  --   unpriced_consumables      {item_name: qty} for items with no catalog price yet
  --                             (unit_price_cents NULL, or an ion_item_id not in the catalog)
  --                             -- a FINITE worklist to drive to zero, never a silent 0.
  -- NOTE: labor buckets by scheduled_date (days CTE); consumables bucket by visit_date.
  -- These agree except at a month boundary where a visit's scheduled/visit date straddle months.
  SELECT task_id, billing_month,
         jsonb_object_agg(item_name, qty) AS consumables,
         COALESCE(sum(amount_cents) FILTER (WHERE amount_cents IS NOT NULL), 0) AS expected_consumable_cents,
         COALESCE(jsonb_object_agg(item_name, qty)
                    FILTER (WHERE amount_cents IS NULL), '{}'::jsonb) AS unpriced_consumables
  FROM (
    SELECT v.task_id, date_trunc('month', v.visit_date)::date AS billing_month,
           cu.item_name, sum(cu.quantity) AS qty,
           (round(sum(cu.quantity) * max(cc.unit_price_cents)))::int AS amount_cents
    FROM maintenance.visits v
    JOIN maintenance.consumables_usage cu ON cu.visit_id = v.id
    LEFT JOIN maintenance.consumables cc ON cc.ion_item_id = cu.ion_item_id
    WHERE v.task_id IS NOT NULL AND cu.item_name IS NOT NULL
    GROUP BY 1, 2, cu.item_name
  ) z
  GROUP BY task_id, billing_month
)
INSERT INTO billing_audit.task_billing_periods
  (task_id, billing_month, qbo_customer_id, service_location_id, billing_method,
   per_visit_rate_cents, flat_rate_monthly_cents, visit_count, billable_visit_count,
   expected_labor_cents, consumables, expected_consumable_cents, unpriced_consumables, status)
   -- expected_total_cents is a GENERATED column (labor + consumable) -- never inserted.
SELECT tm.task_id, tm.billing_month, tl.qbo_customer_id, tl.service_location_id, g.billing_method,
       g.per_visit_rate_cents, g.flat_rate_monthly_cents,
       COALESCE(days.visit_count, 0), COALESCE(days.billable_visit_count, 0),
       CASE WHEN g.billing_method = 'flat_rate_monthly'
            -- FLAT task: bills its flat monthly amount regardless of how many (or zero)
            -- visits are attributed to it this month.
            THEN COALESCE(g.flat_rate_monthly_cents, 0)
            -- Everything else (per_visit, one-time, QC): the TASK's contracted rate x
            -- billable days. The rate lives on the task (one ION contract = one rate);
            -- a QC task carries rate 0, so its days multiply out to $0 labor.
            ELSE COALESCE(g.per_visit_rate_cents, 0) * COALESCE(days.billable_visit_count, 0)
            END AS expected_labor_cents,
       cons.consumables,
       COALESCE(cons.expected_consumable_cents, 0),
       COALESCE(cons.unpriced_consumables, '{}'::jsonb),
       'visits_accruing'
FROM task_month tm
JOIN task_loc tl ON tl.task_id = tm.task_id
LEFT JOIN gov g  ON g.task_id   = tm.task_id AND g.billing_month   = tm.billing_month
LEFT JOIN days   ON days.task_id = tm.task_id AND days.billing_month = tm.billing_month
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
   expected_consumable_cents = EXCLUDED.expected_consumable_cents,
   unpriced_consumables      = EXCLUDED.unpriced_consumables,
   updated_at             = now()
WHERE task_billing_periods.locked_at IS NULL;   -- never mutate a finalized row
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
AND tbp.locked_at IS NULL   -- never delete a finalized row
"""

SUMMARY = """
SELECT to_char(billing_month,'YYYY-MM') AS month,
       count(*) AS promises,
       count(DISTINCT qbo_customer_id) AS customers,
       sum(billable_visit_count) AS billable_visits,
       round(sum(expected_labor_cents)/100.0, 2) AS expected_labor_usd,
       round(sum(expected_consumable_cents)/100.0, 2) AS expected_chem_usd,
       round(sum(expected_total_cents)/100.0, 2) AS expected_total_usd,
       count(*) FILTER (WHERE unpriced_consumables <> '{}'::jsonb) AS rows_with_unpriced,
       count(*) FILTER (WHERE billing_method='flat_rate_monthly') AS flat,
       count(*) FILTER (WHERE billing_method='per_visit') AS per_visit,
       count(*) FILTER (WHERE locked_at IS NOT NULL) AS locked
FROM billing_audit.task_billing_periods
GROUP BY billing_month ORDER BY billing_month;
"""


def main(supabase_connection, dry_run=True, lock_through=None):
    """Rebuild the OPEN months' promises (locked months are skipped -- see locked_months CTE).

    Designed to run continuously: daily / any time mid-month re-UPSERTs the current month so
    the table is a live picture of where billing stands (mid-month revenue = SUM(expected_total_cents)
    over the current month; high-bill early-warning = per-customer expected_total this month).

    lock_through (date, optional): after building, finalize every month STRICTLY BEFORE this date
    by stamping locked_at (only rows not already locked). Call this once a month has been billed +
    reconciled so the builder stops reprocessing it. e.g. lock_through='2026-06-01' locks May and
    earlier. Idempotent -- re-locking an already-locked month is a no-op.
    """
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute(UPSERT)
            upserted = cur.rowcount
            cur.execute(DELETE_ORPHANS)
            orphans_deleted = cur.rowcount
            locked = 0
            if lock_through:
                cur.execute(
                    "UPDATE billing_audit.task_billing_periods SET locked_at = now(), updated_at = now() "
                    "WHERE billing_month < %s AND locked_at IS NULL", (lock_through,))
                locked = cur.rowcount
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
                "rows_locked": locked, "total_rows_after": total, "by_month": by_month}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
