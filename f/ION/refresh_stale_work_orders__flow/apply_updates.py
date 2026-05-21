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


def main(previous_result: dict):
    results = previous_result.get("results", [])
    if not results:
        return {"refreshed": 0, "changed": 0, "errors": 0, "changes": []}

    conn = get_db_conn()
    cur = conn.cursor()
    changed_count = 0
    error_count = 0
    changes = []

    for r in results:
        wo = r.get("wo_number")
        inv = r.get("invoice_number")
        sched = r.get("schedule_status")
        if r.get("error"):
            error_count += 1
            continue
        if not wo:
            continue

        cur.execute(
            "SELECT invoice_number, schedule_status FROM public.work_orders WHERE wo_number = %s",
            (wo,),
        )
        row = cur.fetchone()
        if not row:
            # Not in DB; refresh script doesn't insert. Discovery is a separate concern.
            continue
        cur_inv, cur_sched = row

        changes_this_row = []
        if inv and inv != cur_inv:
            changes_this_row.append(f"invoice_number {cur_inv} -> {inv}")
        if sched and sched != cur_sched:
            changes_this_row.append(f"schedule_status {cur_sched} -> {sched}")

        # COALESCE — never clobber a non-NULL DB value with NULL from refresh.
        cur.execute(
            """
            UPDATE public.work_orders
               SET invoice_number    = COALESCE(%s, invoice_number),
                   schedule_status   = COALESCE(%s, schedule_status),
                   last_refreshed_at = now()
             WHERE wo_number = %s
            """,
            (inv, sched, wo),
        )

        if changes_this_row:
            changed_count += 1
            changes.append({"wo_number": wo, "changes": changes_this_row})
            print(f"  WO {wo}: {', '.join(changes_this_row)}")

    conn.commit()
    cur.close()
    conn.close()

    return {
        "refreshed": len(results) - error_count,
        "changed": changed_count,
        "errors": error_count,
        "changes": changes,
    }
