# requirements:
# psycopg2-binary

"""
f/ION/_lib/taskless_visits

Step 1 of the authoritative log-based visit->task linker. Returns visits that
still have task_id NULL, with the visit's scheduled_date + an ION customer-id
hint (from any existing task at the same service_location -- its
external_data.ion_cust_id IS the ION customerid used to prime customerTabs).
Step b resolves the log (loglist -> LogID -> addLog -> EventID) and step c links.
"""

from f.ION._lib.upsert import _connect


def main(supabase_connection):
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.id::text, v.service_location_id, c.display_name, sl.street,
                       v.scheduled_date, v.visit_date,
                       (SELECT t.external_data->>'ion_cust_id'
                        FROM maintenance.tasks t
                        WHERE t.service_location_id = v.service_location_id
                          AND t.external_data->>'ion_cust_id' IS NOT NULL
                        LIMIT 1) AS ion_cust_hint
                FROM maintenance.visits v
                JOIN public.service_locations sl ON sl.id = v.service_location_id
                JOIN public."Customers" c ON c.id = sl.account_id
                WHERE v.task_id IS NULL
            """)
            visits = [{
                "visit_id": r[0], "service_location_id": r[1], "name": r[2], "street": r[3],
                "scheduled_date": r[4].isoformat() if r[4] else None,
                "visit_date": r[5].isoformat() if r[5] else None,
                "ion_cust_hint": r[6],
            } for r in cur.fetchall()]
        return {"count": len(visits), "visits": visits}
    finally:
        conn.close()
