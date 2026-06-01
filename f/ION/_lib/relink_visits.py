# requirements:
# psycopg2-binary

"""
f/ION/_lib/relink_visits

Re-link existing maintenance.visits to tasks + schedule slots now that the
recurring-task sync (#58) and schedule-slot sync (#59) populated tasks +
task_schedules with real day_of_week/tech.

WHY: many visits were ingested BEFORE their task existed (task_id NULL) or
before the schedule slots had days/tech (task_schedule_id NULL because the
ingestion resolver matched on day+tech that weren't populated yet). This
backfills those links against the now-clean reference data.

RESOLUTION (per visit):
  task_id          -> the location's OPEN task (one open task per service_location;
                      tasks_one_open_per_loc).
  task_schedule_id -> among that task's ACTIVE slots, the slot whose day_of_week =
                      the visit's weekday; if several share that weekday (merged
                      multi-task location, e.g. pool + QC), disambiguate by
                      tech (== actual_tech), then by price (== visit price), else
                      first. No same-weekday slot -> leave NULL (off-cadence visit:
                      make-up / coverage / extra -- a real signal, not an error).

SAFETY: fill-only by default (sets task_id / task_schedule_id only where NULL;
never overwrites an existing link) unless overwrite=True. dry_run=True default ->
all writes in one transaction, then ROLLBACK.

Public API:
    relink(supabase_connection, dry_run=True, overwrite=False, since=None) -> stats
"""

from collections import defaultdict
from datetime import date as _date, datetime

from f.ION._lib.upsert import _connect


def _pg_dow(d):
    """Postgres DOW convention: 0=Sun .. 6=Sat (Python weekday: 0=Mon .. 6=Sun)."""
    if isinstance(d, str):
        try:
            d = _date.fromisoformat(d[:10])
        except ValueError:
            return None
    if isinstance(d, datetime):
        d = d.date()
    return (d.weekday() + 1) % 7


def relink(supabase_connection, dry_run=True, overwrite=False, since=None):
    conn = _connect(supabase_connection)
    stats = {
        "visits_scanned": 0,
        "task_linked": 0,         # newly set task_id
        "schedule_linked": 0,     # newly set task_schedule_id
        "still_no_task": 0,       # location has no open task (Group B)
        "off_schedule": 0,        # has task but visit weekday not in any slot
        "match_tier": defaultdict(int),  # how the slot was chosen
        "dry_run": dry_run,
        "overwrite": overwrite,
        "committed": False,
    }
    try:
        # location -> open task
        open_task_by_loc = {}
        with conn.cursor() as cur:
            cur.execute("SELECT service_location_id, id FROM maintenance.tasks WHERE status IN ('active','paused')")
            for sl, tid in cur.fetchall():
                open_task_by_loc.setdefault(sl, tid)

        # task -> active schedule slots
        sched_by_task = defaultdict(list)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT task_id, id, day_of_week, tech_employee_id, price_per_visit_cents
                FROM maintenance.task_schedules
                WHERE active AND day_of_week IS NOT NULL
            """)
            for tid, sid, dow, tech, ppv in cur.fetchall():
                sched_by_task[tid].append({"id": sid, "dow": dow, "tech": tech, "ppv": ppv})

        # visits needing a link
        where = "(task_id IS NULL OR task_schedule_id IS NULL)"
        params = []
        if since:
            where += " AND visit_date >= %s"
            params.append(since)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT id, service_location_id, visit_date, actual_tech_id,
                       task_id, task_schedule_id, price_cents
                FROM maintenance.visits
                WHERE {where}
            """, params)
            visits = cur.fetchall()

        with conn.cursor() as cur:
            for vid, sl, vdate, atech, cur_task, cur_sched, vprice in visits:
                stats["visits_scanned"] += 1

                # resolve task
                new_task = cur_task
                if cur_task is None:
                    t = open_task_by_loc.get(sl)
                    if t is None:
                        stats["still_no_task"] += 1
                        continue  # nothing to do without a task
                    new_task = t

                # resolve schedule slot (if missing or overwriting)
                chosen_sched = None
                if new_task is not None and (cur_sched is None or overwrite):
                    dow = _pg_dow(vdate)
                    cands = [s for s in sched_by_task.get(new_task, []) if s["dow"] == dow] if dow is not None else []
                    if len(cands) == 1:
                        chosen_sched = cands[0]["id"]; stats["match_tier"]["single_day"] += 1
                    elif len(cands) > 1:
                        by_tech = next((s for s in cands if s["tech"] and s["tech"] == atech), None)
                        by_price = next((s for s in cands if s["ppv"] and vprice and s["ppv"] == vprice), None)
                        if by_tech:
                            chosen_sched = by_tech["id"]; stats["match_tier"]["day_tech"] += 1
                        elif by_price:
                            chosen_sched = by_price["id"]; stats["match_tier"]["day_price"] += 1
                        else:
                            chosen_sched = cands[0]["id"]; stats["match_tier"]["day_first"] += 1

                set_task = (cur_task is None and new_task is not None)
                set_sched = (chosen_sched is not None and chosen_sched != cur_sched)

                if new_task is not None and chosen_sched is None and cur_sched is None:
                    stats["off_schedule"] += 1

                if set_task or set_sched:
                    cur.execute(
                        """UPDATE maintenance.visits
                           SET task_id          = COALESCE(%s, task_id),
                               task_schedule_id = COALESCE(%s, task_schedule_id),
                               updated_at = now()
                           WHERE id = %s""",
                        (new_task if set_task else None, chosen_sched if set_sched else None, vid),
                    )
                    if set_task:
                        stats["task_linked"] += 1
                    if set_sched:
                        stats["schedule_linked"] += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True

        stats["match_tier"] = dict(stats["match_tier"])
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(supabase_connection, dry_run=True, overwrite=False, since=None):
    """Backfill task_id + task_schedule_id on existing visits.
    dry_run default rolls back. overwrite=True re-resolves existing schedule links too.
    since (ISO date) limits to recent visits; None = all.
    """
    return relink(supabase_connection, dry_run=dry_run, overwrite=overwrite, since=since)
