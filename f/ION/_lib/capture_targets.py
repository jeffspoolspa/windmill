# requirements:
# psycopg2-binary

"""
f/ION/_lib/capture_targets

Step 1 of the non-active-task capture (every-visit-should-have-a-task). Returns
the service_locations that STILL have task-less visits after the recurring-task
sync + relink. For each we pull the customer's FULL ION taskList (incl. expired /
one-time / blank-address tasks that #58 -- active-recurring only -- didn't sync),
then create the missing maintenance.tasks and link the visits (step b + c).
"""

from f.ION._lib.upsert import _connect


def main(supabase_connection):
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.service_location_id, c.display_name, sl.street, sl.city, sl.account_id,
                       count(*) AS visits, min(v.visit_date) AS first_v, max(v.visit_date) AS last_v
                FROM maintenance.visits v
                JOIN public.service_locations sl ON sl.id = v.service_location_id
                JOIN public."Customers" c ON c.id = sl.account_id
                WHERE v.task_id IS NULL
                GROUP BY 1,2,3,4,5
                ORDER BY count(*) DESC
            """)
            targets = [{
                "service_location_id": r[0], "name": r[1], "street": r[2], "city": r[3],
                "account_id": r[4], "visits": r[5],
                "first_v": r[6].isoformat() if r[6] else None, "last_v": r[7].isoformat() if r[7] else None,
            } for r in cur.fetchall()]
        return {"count": len(targets), "targets": targets}
    finally:
        conn.close()
