# requirements:
# psycopg2-binary

"""
f/ION/_lib/link_visits_by_event

Step 3 of the authoritative log-based linker. Takes {visit_id, event_id} from
f/ION/api/resolve_visit_tasks_via_log (EventID = the ION task id recorded on the
service log) and sets maintenance.visits.task_id to the task whose
task_schedules.ion_task_id = event_id. This is ground truth -- it overrides the
day/window inference (e.g. ION logged a visit against a task whose recorded
window doesn't cover the visit date).

sl_mismatch flags links where the EventID's task lives on a different
service_location than the visit (e.g. PARRISH's task stranded on a junk sl) --
the link is still correct (EventID is authoritative); the sl is a separate cleanup.
event_not_in_db = EventID we haven't synced (would need a get_task_detail capture).

SAFETY: dry_run=True default -> rolls back.
"""

from f.ION._lib.upsert import _connect


def link(links, supabase_connection, dry_run=True):
    conn = _connect(supabase_connection)
    stats = {"links": len(links), "linked": 0, "no_event": 0, "event_not_in_db": 0,
             "sl_mismatch": 0, "examples": [], "dry_run": dry_run, "committed": False}
    try:
        m = {}  # ion_task_id -> (task_id, service_location_id)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts.ion_task_id, ts.task_id, t.service_location_id
                FROM maintenance.task_schedules ts
                JOIN maintenance.tasks t ON t.id = ts.task_id
                WHERE ts.ion_task_id IS NOT NULL
            """)
            for iid, tid, sl in cur.fetchall():
                m.setdefault(iid, (tid, sl))

        with conn.cursor() as cur:
            for lk in links:
                eid = str(lk.get("event_id") or "").strip()
                vid = lk.get("visit_id")
                if not eid:
                    stats["no_event"] += 1
                    continue
                if eid not in m:
                    stats["event_not_in_db"] += 1
                    if len(stats["examples"]) < 15:
                        stats["examples"].append({"visit_id": vid, "event_id": eid, "note": "task not in DB"})
                    continue
                tid, tsl = m[eid]
                cur.execute(
                    "UPDATE maintenance.visits SET task_id=%s, updated_at=now() WHERE id=%s AND task_id IS NULL",
                    (tid, vid),
                )
                if cur.rowcount:
                    stats["linked"] += 1
                    if tsl != lk.get("sl"):
                        stats["sl_mismatch"] += 1
                        if len(stats["examples"]) < 15:
                            stats["examples"].append({"visit_id": vid, "event_id": eid, "task_sl": tsl, "visit_sl": lk.get("sl"), "note": "linked; task on different sl"})

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True
        return stats
    finally:
        conn.close()


def main(links, supabase_connection, dry_run=True):
    """Link visits to the task identified by the service log's EventID. dry_run rolls back."""
    return link(links, supabase_connection, dry_run=dry_run)
