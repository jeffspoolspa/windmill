# requirements:
# psycopg2-binary

"""
f/ION/_lib/schedule_target_custids

Flow step 1 for the schedule-slot sync (#59): return the ION customer ids to
pull taskList for.
  only_dayless=True (default): just customers that have an active task_schedules
    slot with day_of_week IS NULL -- the gap the active-tasks sync left. Fast,
    focused fix.
  only_dayless=False: every active ION customer -> full schedule re-derive.
"""

from f.ION._lib.upsert import _connect


def main(supabase_connection, only_dayless=True):
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            if only_dayless:
                cur.execute("""
                    SELECT DISTINCT t.external_data->>'ion_cust_id'
                    FROM maintenance.task_schedules ts
                    JOIN maintenance.tasks t ON t.id = ts.task_id
                    WHERE ts.active AND ts.day_of_week IS NULL
                      AND t.external_source = 'ion'
                      AND t.external_data->>'ion_cust_id' IS NOT NULL
                """)
            else:
                cur.execute("""
                    SELECT DISTINCT t.external_data->>'ion_cust_id'
                    FROM maintenance.tasks t
                    WHERE t.external_source = 'ion' AND t.status IN ('active','paused')
                      AND t.external_data->>'ion_cust_id' IS NOT NULL
                """)
            ids = [r[0] for r in cur.fetchall() if r[0]]
        return {"only_dayless": only_dayless, "count": len(ids), "cust_ids": ids}
    finally:
        conn.close()
