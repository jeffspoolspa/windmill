# requirements:
# psycopg2-binary

"""
f/ION/_lib/upsert_nonactive_tasks

Step 3 of the non-active-task capture. Takes resolved rows from
f/ION/api/resolve_customer_tasks ({service_location_id, ion_customerid, tasks:[
full CustomerTask incl. expired]}) and, for each ION task NOT already in our DB
(by ion_task_id), creates a maintenance.tasks row at that service_location +
schedule slots, then links the location's task-less visits to it by date window.

STATUS: expired ION task -> 'closed'; still-active -> 'active' (but if the
location already has an open task, fall back to 'closed' to respect
tasks_one_open_per_loc -- these are historical/one-time anyway). This is how the
one-time cleans / green-pool / expired-maintenance jobs (which #58's active-only
sync skips) get represented so every visit has a task.

VISIT LINK: a task-less visit at the location links to the captured task whose
[starts_on, ends_on] window contains the visit_date (latest-starting wins; single
captured task is the fallback). Schedule slot matched by weekday when present.

SAFETY: dry_run=True default -> all writes in one transaction, then ROLLBACK.

Public API:
    capture(rows, supabase_connection, dry_run=True) -> stats
"""

from collections import defaultdict
from datetime import date as _date
import json

from f.ION._lib.upsert import _connect
from f.ION._lib.upsert_tasks import parse_ion_date
from f.ION._lib.upsert_schedules import _build_tech_resolver, _resolve_tech, _map_frequency


def _d(iso):
    if not iso:
        return None
    try:
        return _date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return None


def capture(rows, supabase_connection, dry_run=True, source="ion"):
    conn = _connect(supabase_connection)
    stats = {
        "rows": len(rows),
        "tasks_created": 0,
        "slots_created": 0,
        "visits_linked": 0,
        "skipped_existing_iontask": 0,
        "skipped_no_iontask": 0,
        "visits_unmatched_window": 0,
        "by_status": defaultdict(int),
        "by_service": defaultdict(int),
        "dry_run": dry_run,
        "committed": False,
    }
    try:
        by_full, by_suffix = _build_tech_resolver(conn)
        existing = set()
        open_by_loc = {}
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT ion_task_id FROM maintenance.task_schedules WHERE ion_task_id IS NOT NULL")
            existing = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT service_location_id, id FROM maintenance.tasks WHERE status IN ('active','paused')")
            for sl, tid in cur.fetchall():
                open_by_loc.setdefault(sl, tid)

        with conn.cursor() as cur:
            for row in rows:
                sl = row.get("service_location_id")
                tasks = row.get("tasks") or []
                if not sl:
                    continue
                created = []  # {tid, starts(date), ends(date), days:set}

                for t in tasks:
                    iid = str(t.get("ionTaskId") or "").strip()
                    if not iid:
                        stats["skipped_no_iontask"] += 1
                        continue
                    if iid in existing:
                        stats["skipped_existing_iontask"] += 1
                        continue

                    expired = bool(t.get("expired"))
                    status = "closed" if expired else "active"
                    if status == "active" and sl in open_by_loc:
                        status = "closed"  # can't be a 2nd open task; treat as historical
                    desc = (t.get("description") or "")
                    starts = _d(parse_ion_date(t.get("taskStarts")))
                    ends = _d(parse_ion_date(t.get("taskExpires")))
                    freq = _map_frequency(t.get("recurrence"), t.get("weekParity"))
                    tech = _resolve_tech(by_full, by_suffix, t.get("assignedTo"))
                    ext = {
                        "ion_cust_id": row.get("ion_customerid") or "",
                        "service_type": desc,
                        "recurrence": t.get("recurrence") or "",
                        "one_time": "ONE TIME" in desc.upper(),
                        "captured": "nonactive",
                    }
                    cur.execute(
                        """INSERT INTO maintenance.tasks
                             (service_location_id, status, starts_on, ends_on, external_source, external_data)
                           VALUES (%s, %s, COALESCE(%s, CURRENT_DATE), %s, %s, %s::jsonb)
                           RETURNING id""",
                        (sl, status, starts.isoformat() if starts else None,
                         ends.isoformat() if ends else None, source, json.dumps(ext)),
                    )
                    tid = cur.fetchone()[0]
                    existing.add(iid)
                    if status == "active":
                        open_by_loc[sl] = tid
                    stats["tasks_created"] += 1
                    stats["by_status"][status] += 1
                    svc = desc.split(" - ")[0][:24] if desc else "(none)"
                    stats["by_service"][svc] += 1

                    days = sorted(set(int(d) for d in (t.get("activeDays") or []) if d is not None))
                    for d in days:
                        cur.execute(
                            """INSERT INTO maintenance.task_schedules
                                 (task_id, ion_task_id, day_of_week, tech_employee_id, frequency,
                                  active, starts_on, ends_on, external_source)
                               VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, CURRENT_DATE), %s, %s)""",
                            (tid, iid, d, tech, freq, status != "closed",
                             starts.isoformat() if starts else None,
                             ends.isoformat() if ends else None, source),
                        )
                        stats["slots_created"] += 1
                    created.append({"tid": tid, "starts": starts, "ends": ends, "days": set(days)})

                if not created:
                    continue

                cur.execute(
                    "SELECT id, visit_date FROM maintenance.visits WHERE service_location_id=%s AND task_id IS NULL",
                    (sl,),
                )
                for vid, vdate in cur.fetchall():
                    best = None
                    for ct in created:
                        s, e = ct["starts"], ct["ends"]
                        if s and vdate >= s and (e is None or vdate <= e):
                            if best is None or (ct["starts"] or _date.min) > (best["starts"] or _date.min):
                                best = ct
                    if best is None and len(created) == 1:
                        best = created[0]
                    if best is None:
                        stats["visits_unmatched_window"] += 1
                        continue
                    cur.execute(
                        "UPDATE maintenance.visits SET task_id=%s, updated_at=now() WHERE id=%s",
                        (best["tid"], vid),
                    )
                    stats["visits_linked"] += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True

        stats["by_status"] = dict(stats["by_status"])
        stats["by_service"] = dict(stats["by_service"])
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(rows, supabase_connection, dry_run=True, source="ion"):
    """Create maintenance.tasks for non-active (expired/one-time) ION tasks of
    task-less-visit customers + link their visits. dry_run default rolls back.
    rows: output of f/ION/api/resolve_customer_tasks (.rows)."""
    return capture(rows, supabase_connection, dry_run=dry_run, source=source)
