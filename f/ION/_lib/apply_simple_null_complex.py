# requirements:
# wmill
# psycopg2-binary

"""
f/ION/_lib/apply_simple_null_complex  --  durable resolver, step 3a.

Takes the classifier output (resolve_visit_targets):
  - simple_assignments [{visit_id, ion_task_id, task_id}] -> link directly.
  - complex_targets [{visit_id, ...}] -> NULL their task_id/ion_task_id so the
    authoritative ION-log linker (resolve_visit_tasks_via_log + link_visits_by_event)
    can re-set them from the EventID (link_visits_by_event only writes where
    task_id IS NULL). Capturing the customers' missing tasks (upsert_nonactive_tasks)
    runs between this and the log-link.

SAFETY: dry_run=True default -> rollback.
"""

import wmill
from f.ION._lib.upsert import _connect


def main(simple_assignments, complex_targets, supabase_connection=None, dry_run: bool = True):
    if supabase_connection is None:
        supabase_connection = wmill.get_resource("u/carter/supabase")
    conn = _connect(supabase_connection)
    applied = 0
    try:
        with conn.cursor() as cur:
            for a in (simple_assignments or []):
                if not a.get("task_id"):
                    continue
                cur.execute(
                    "UPDATE maintenance.visits SET ion_task_id=%s, task_id=%s, updated_at=now() WHERE id=%s",
                    (a.get("ion_task_id"), a["task_id"], a["visit_id"]),
                )
                applied += cur.rowcount
            cids = sorted({t["visit_id"] for t in (complex_targets or [])})
            nulled = 0
            if cids:
                cur.execute(
                    "UPDATE maintenance.visits SET task_id=NULL, ion_task_id=NULL, updated_at=now() WHERE id = ANY(%s)",
                    (cids,),
                )
                nulled = cur.rowcount
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {"applied_simple": applied, "nulled_complex": nulled,
                "complex_visits": len(cids), "dry_run": dry_run, "committed": not dry_run}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
