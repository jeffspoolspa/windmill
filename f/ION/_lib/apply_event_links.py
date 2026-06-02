# requirements:
# wmill
# psycopg2-binary

"""
f/ION/_lib/apply_event_links  --  durable resolver, final link step.

Carter's rule: pull the customer's FULL task list FIRST (complete the task table),
THEN guarantee service-log linkage. So this runs AFTER the task-list pull/upsert,
and the EventID (the ion_task_id ION recorded on the service log) is GROUND TRUTH:
it OVERWRITES any prior/window-inferred task_id (not just NULLs). Because the task
list was completed first, every EventID should resolve to a task we have; any that
don't (event_not_in_db) or have no log (no_event) are flagged, never mis-linked.

links: [{visit_id, event_id}] from f/ION/api/resolve_visit_tasks_via_log.
SAFETY: dry_run=True default -> rollback.
"""

import wmill
from f.ION._lib.upsert import _connect


def main(links, supabase_connection=None, dry_run: bool = True):
    if supabase_connection is None:
        supabase_connection = wmill.get_resource("u/carter/supabase")
    conn = _connect(supabase_connection)
    stats = {"links": len(links or []), "linked": 0, "no_event": 0,
             "event_not_in_db": 0, "examples": [], "dry_run": dry_run, "committed": False}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts.ion_task_id, ts.task_id FROM maintenance.task_schedules ts "
                "WHERE ts.ion_task_id IS NOT NULL"
            )
            m = {}
            for iid, tid in cur.fetchall():
                m.setdefault(iid, tid)
            for lk in (links or []):
                eid = str(lk.get("event_id") or "").strip()
                vid = lk.get("visit_id")
                if not eid:
                    stats["no_event"] += 1
                    continue
                if eid not in m:
                    stats["event_not_in_db"] += 1
                    if len(stats["examples"]) < 20:
                        stats["examples"].append({"visit_id": vid, "event_id": eid})
                    continue
                cur.execute(
                    "UPDATE maintenance.visits SET task_id=%s, ion_task_id=%s, updated_at=now() "
                    "WHERE id=%s::uuid",
                    (m[eid], eid, vid),
                )
                stats["linked"] += cur.rowcount
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
