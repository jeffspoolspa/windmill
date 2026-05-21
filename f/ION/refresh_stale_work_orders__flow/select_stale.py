import psycopg2
import wmill

SUPABASE_RESOURCE = "u/carter/supabase"


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def main(batch_size: int = 200, stale_after_hours: int = 24, wo_number: str | None = None):
    conn = get_db_conn()
    cur = conn.cursor()
    if wo_number:
        cur.execute("SELECT wo_number FROM public.work_orders WHERE wo_number = %s", (wo_number,))
    else:
        cur.execute("""
            SELECT wo_number
              FROM public.work_orders
             WHERE wo_number IS NOT NULL
               AND (last_refreshed_at IS NULL
                    OR last_refreshed_at < now() - (%s || ' hours')::interval)
               AND COALESCE(schedule_status, '') NOT IN ('Cancelled')
             ORDER BY last_refreshed_at NULLS FIRST, wo_number
             LIMIT %s
        """, (str(stale_after_hours), batch_size))
    wos = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    print(f"Selected {len(wos)} stale WOs for refresh")
    return {"wo_numbers": wos}
