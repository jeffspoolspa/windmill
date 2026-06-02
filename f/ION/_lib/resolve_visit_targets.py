# requirements:
# wmill
# psycopg2-binary

"""
f/ION/_lib/resolve_visit_targets  --  durable visit->task resolver, step 1.

Group visits by customer and classify each against the CANONICAL task census
(ion.recurring_tasks) by Carter's rules:
  a CANDIDATE = task where qbo_customer matches, the task was ACTIVE AT THE LOG
  DATE (task_start <= scheduled_date <= task_end), and SAME SERVICE-TYPE FAMILY.
  Grain = 1 visit per (task, day).

A customer is SIMPLE if EVERY visit has exactly ONE candidate -> link 1:1 by
logic (no ION call). Otherwise COMPLEX (some visit has 0 or 2+ candidates, e.g.
green-pool/QC with no task, or a multi-pool community with several same-service
tasks) -> ALL of that customer's visits go to the authoritative ION-log path
(step b: loglist -> addLog -> EventID) to be certain, and any task id we don't
have gets pulled in. Pure logic can't tell one-task-many-pools from
task-per-pool; only the per-log EventID is authoritative.

Returns simple_assignments + complex_targets + stats. Read-only.
"""

import wmill
from f.ION._lib.upsert import _connect

FAM = (r'(?:POOL MAINTENANCE|FLAT RATE|CHEMICAL TESTING|SPA CLEAN|FOUNTAIN CLEAN'
       r'|QUALITY CONTROL|GREEN POOL|HALF HOUR|ONE TIME CLEAN|SALT CELL)')

SQL = """
WITH v AS (
  SELECT mv.id::text AS visit_id, mv.service_location_id, mv.scheduled_date,
         c.display_name AS name, sl.street,
         COALESCE(c.duplicate_of_qbo_id, c.qbo_customer_id) AS qbo,
         UPPER((regexp_match(UPPER(COALESCE(mv.service_type,'')), %(fam)s))[1]) AS svc_fam,
         (SELECT t.external_data->>'ion_cust_id' FROM maintenance.tasks t
            WHERE t.service_location_id = mv.service_location_id
              AND t.external_data->>'ion_cust_id' IS NOT NULL LIMIT 1) AS ion_cust_hint
  FROM maintenance.visits mv
  JOIN public.service_locations sl ON sl.id = mv.service_location_id
  JOIN public."Customers" c ON c.id = sl.account_id
  WHERE (%(month)s::date IS NULL
         OR date_trunc('month', mv.scheduled_date)::date = %(month)s::date)
)
SELECT v.visit_id, v.service_location_id, v.name, v.street,
       v.scheduled_date::text AS scheduled_date, v.ion_cust_hint, v.qbo,
       (SELECT array_agg(rt.ion_task_id) FROM ion.recurring_tasks rt
          WHERE rt.qbo_customer_id = v.qbo
            AND (rt.task_start IS NULL OR rt.task_start <= v.scheduled_date)
            AND (rt.task_end   IS NULL OR rt.task_end   >= v.scheduled_date)
            AND UPPER((regexp_match(UPPER(COALESCE(rt.service_type,'')), %(fam)s))[1]) = v.svc_fam
       ) AS cand_ids
FROM v
"""

TASK_MAP = """
SELECT DISTINCT ON (ts.ion_task_id) ts.ion_task_id, ts.task_id::text
FROM maintenance.task_schedules ts
WHERE ts.ion_task_id IS NOT NULL
ORDER BY ts.ion_task_id, ts.active DESC, ts.updated_at DESC
"""


def main(supabase_connection=None, billing_month: str = "2026-05"):
    if supabase_connection is None:
        supabase_connection = wmill.get_resource("u/carter/supabase")
    month = (billing_month + "-01") if billing_month else None
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute(TASK_MAP)
            task_of = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(SQL, {"fam": FAM, "month": month})
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        by_cust = {}
        for r in rows:
            by_cust.setdefault(r["qbo"], []).append(r)

        simple_assignments, complex_targets = [], []
        n_simple_cust = n_complex_cust = 0
        for qbo, visits in by_cust.items():
            complex_cust = any(len(v["cand_ids"] or []) != 1 for v in visits)
            if complex_cust:
                n_complex_cust += 1
                for v in visits:
                    complex_targets.append({
                        "visit_id": v["visit_id"], "service_location_id": v["service_location_id"],
                        "name": v["name"], "street": v["street"],
                        "scheduled_date": v["scheduled_date"], "ion_cust_hint": v["ion_cust_hint"],
                    })
            else:
                n_simple_cust += 1
                for v in visits:
                    ion_id = (v["cand_ids"] or [None])[0]
                    simple_assignments.append({
                        "visit_id": v["visit_id"], "ion_task_id": ion_id,
                        "task_id": task_of.get(ion_id),
                    })
        return {
            "billing_month": billing_month,
            "stats": {
                "visits": len(rows),
                "simple_customers": n_simple_cust, "complex_customers": n_complex_cust,
                "simple_assignments": len(simple_assignments),
                "complex_target_visits": len(complex_targets),
                "simple_missing_task_id": sum(1 for a in simple_assignments if not a["task_id"]),
            },
            "simple_assignments": simple_assignments,
            "complex_targets": complex_targets,
        }
    finally:
        conn.close()
