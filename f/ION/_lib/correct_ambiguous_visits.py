# requirements:
# psycopg2-binary

"""
f/ION/_lib/correct_ambiguous_visits

Step 3 of the EventID correction pass for rate-AMBIGUOUS locations (>1 active task
at the same rate, e.g. WINDING RIVER's two $50 chem + two $85 tasks). The bulk
report can't split these, so the upsert attributed each visit to ONE same-rate task
arbitrarily. This corrects ion_task_id + task_id (and is_serviceable) using the
GROUND-TRUTH EventID that ION recorded on each service log.

Input `links`: list of {service_location_id, scheduled_date (YYYY-MM-DD),
timein ("HH:MM AM"), event_id, serviceable} from f/ION/api/resolve_ambiguous_eventids
(loglist -> addLog). Alignment to the stored per-log visit is by
(service_location_id, scheduled_date, started_at) where started_at = the same
date+timein combination the upsert stored. Multiple pool rows serviced at the same
clock time under one task all get the same event_id (correct — same task).

SAFETY: dry_run=True default -> rolls back.
"""

from datetime import date as _date, datetime

from f.ION._lib.upsert import _connect


def _combine(d, time_str):
    """date(YYYY-MM-DD) + 'HH:MM AM/PM' -> naive isoformat (matches upsert's started_at)."""
    if not d or not time_str:
        return None
    try:
        dd = _date.fromisoformat(d) if isinstance(d, str) else d
        t = datetime.strptime(time_str.strip(), "%I:%M %p").time()
        return datetime.combine(dd, t).isoformat()
    except (ValueError, TypeError):
        return None


def main(links, supabase_connection, dry_run=True):
    conn = _connect(supabase_connection)
    stats = {"links": len(links), "updated": 0, "event_not_in_db": 0,
             "no_match": 0, "examples": [], "dry_run": dry_run, "committed": False}
    try:
        # ion_task_id -> task_id (prefer tasks.ion_task_id, else schedule's)
        ion_to_task = {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(t.ion_task_id, ts.ion_task_id) AS iid, t.id
                FROM maintenance.tasks t
                LEFT JOIN maintenance.task_schedules ts ON ts.task_id = t.id
                WHERE COALESCE(t.ion_task_id, ts.ion_task_id) IS NOT NULL
            """)
            for iid, tid in cur.fetchall():
                ion_to_task.setdefault(str(iid), tid)

        with conn.cursor() as cur:
            for lk in links:
                eid = str(lk.get("event_id") or "").strip()
                sl = lk.get("service_location_id")
                d = lk.get("scheduled_date")
                timein = lk.get("timein")
                serviceable = lk.get("serviceable")
                if not eid or not sl or not d:
                    continue
                if eid not in ion_to_task:
                    stats["event_not_in_db"] += 1
                    if len(stats["examples"]) < 15:
                        stats["examples"].append({"event_id": eid, "note": "task not in DB"})
                    continue
                task_id = ion_to_task[eid]
                started = _combine(d, timein)
                cur.execute("""
                    UPDATE maintenance.visits
                       SET ion_task_id = %s,
                           task_id = %s,
                           is_serviceable = COALESCE(%s, is_serviceable),
                           updated_at = now()
                     WHERE service_location_id = %s
                       AND scheduled_date = %s
                       AND (%s::timestamptz IS NULL OR started_at = %s::timestamptz)
                """, (eid, task_id, serviceable, sl, d, started, started))
                if cur.rowcount:
                    stats["updated"] += cur.rowcount
                else:
                    stats["no_match"] += 1
                    if len(stats["examples"]) < 15:
                        stats["examples"].append({"sl": sl, "date": d, "timein": timein,
                                                  "event_id": eid, "note": "no visit matched"})

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True
        return stats
    finally:
        conn.close()
