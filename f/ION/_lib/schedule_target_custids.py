# requirements:
# psycopg2-binary

"""
f/ION/_lib/schedule_target_custids

Flow step 1 for the schedule-slot sync (#59): return the ION customer ids to
pull taskList for.
  only_dayless=True (default): customers needing day-level re-derive --
    (a) an active slot with day_of_week IS NULL (the gap the active-tasks sync
        left), or
    (b) an active task with ZERO active slots (a task returning from expired:
        upsert_tasks stands slots down but never resurrects day-slots -- day
        activation is THIS pipeline's job, so such customers must be targeted
        or the task stays scheduled nowhere forever).
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
                    UNION
                    SELECT DISTINCT t.external_data->>'ion_cust_id'
                    FROM maintenance.tasks t
                    WHERE t.external_source = 'ion' AND t.status IN ('active','paused')
                      AND t.external_data->>'ion_cust_id' IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM maintenance.task_schedules ts
                        WHERE ts.task_id = t.id AND ts.active
                      )
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
