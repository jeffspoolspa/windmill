# requirements:
# psycopg2-binary

"""
f/ION/_lib/upsert_schedules

Schedule day/tech derivation (#59). Consumes parsed taskList rows (from
f/ION/api/list_customer_tasks, which imports f/ION/_lib/customer_tasks) and
reconciles maintenance.task_schedules so each active ION task has the right
day_of_week + tech_employee_id + frequency.

WHY: the active-tasks sync (f/ION/_lib/upsert_tasks) creates schedule slots with
day_of_week=NULL because the RecurringtasksActive report has no day/tech.
taskList.cfm DOES (Weekly -> bolded weekday letters; Bi-Weekly/Monthly ->
weekday of Next Service + iso-week parity for A/B; Daily -> all days; plus the
assigned tech). This fills those dayless slots and refreshes tech.

KEY = ion_task_id (1:1 with a maintenance.task; rows carry it). For each task:
  desired days = activeDays. Reconcile its active slots:
    - a slot already on a desired day  -> update tech + frequency
    - a dayless (day IS NULL) active slot -> CLAIM it for a desired day
    - still-missing desired day         -> INSERT a new slot on that task
  (Focused/dayless mode does NOT deactivate existing dated slots -- it only fills
   gaps + refreshes tech, so it can't remove a legit day if taskList disagrees.
   Full re-derive mode (full_reconcile=True) also deactivates dated slots whose
   day is no longer in the report.)
  expired tasks are skipped (the active-tasks sync closes them).

TECH: public.employees.ion_username (TEXT[]) entries equal the taskList
"Assigned To" string, but the route prefix drifts ("MNT-C KF, KOREY" vs stored
"MNT-B KF, KOREY") -> match full string, else the route-stripped suffix
("KF, KOREY"). "-A ASSIGN PEND"/"ASSIGN PEND" = unassigned -> null.

FREQUENCY: Weekly->weekly; Bi-Weekly-> biweekly_a if weekParity==0 else
biweekly_b; Daily->daily; Monthly->monthly.

SAFETY: dry_run=True default -> all writes in one transaction, then ROLLBACK.

Public API:
    sync_schedules(rows, supabase_connection, dry_run=True, full_reconcile=False) -> stats
"""

from collections import defaultdict
import psycopg2

from f.ION._lib.upsert import _connect


def _route_stripped(s):
    """'MNT-C KF, KOREY' -> 'KF, KOREY' (drop the route-code prefix before 1st space)."""
    s = (s or "").strip()
    i = s.find(" ")
    return s[i + 1:].strip() if i > 0 else s


def _build_tech_resolver(conn):
    by_full = {}
    by_suffix = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, ion_username FROM public.employees WHERE ion_username IS NOT NULL")
        for emp_id, usernames in cur.fetchall():
            for u in (usernames or []):
                full = (u or "").strip().upper()
                if not full:
                    continue
                by_full.setdefault(full, emp_id)
                by_suffix.setdefault(_route_stripped(full), emp_id)
    return by_full, by_suffix


def _resolve_tech(by_full, by_suffix, assigned_to):
    a = (assigned_to or "").strip()
    if not a or "ASSIGN PEND" in a.upper():
        return None
    up = a.upper()
    if up in by_full:
        return by_full[up]
    return by_suffix.get(_route_stripped(up))


def _map_frequency(recurrence, week_parity):
    r = (recurrence or "").strip().lower().replace("-", "")
    if r == "weekly":
        return "weekly"
    if r == "biweekly":
        return "biweekly_a" if (week_parity == 0) else "biweekly_b"
    if r == "daily":
        return "daily"
    if r == "monthly":
        return "monthly"
    return None  # unknown cadence -> leave frequency untouched


def sync_schedules(rows, supabase_connection, dry_run=True, full_reconcile=False, source="ion"):
    conn = _connect(supabase_connection)
    stats = {
        "rows_total": len(rows),
        "skipped_expired": 0,
        "unmatched_iontask": 0,
        "unmatched_examples": [],
        "slots_dayfilled": 0,       # claimed a dayless slot for a real day
        "slots_inserted": 0,        # added a missing day slot
        "slots_updated": 0,         # refreshed tech/frequency on an existing dated slot
        "slots_deactivated": 0,     # surplus dayless / full_reconcile drops
        "tech_resolved": 0,
        "tech_unresolved": 0,
        "tech_unresolved_examples": [],
        "by_frequency": defaultdict(int),
        "dry_run": dry_run,
        "full_reconcile": full_reconcile,
        "committed": False,
    }
    try:
        by_full, by_suffix = _build_tech_resolver(conn)

        # existing slots per ion_task_id
        sched = {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ion_task_id, id, task_id, day_of_week, active
                FROM maintenance.task_schedules
                WHERE ion_task_id IS NOT NULL
            """)
            for ion_task_id, sid, task_id, dow, active in cur.fetchall():
                e = sched.setdefault(ion_task_id, {"task_id": task_id, "slots": []})
                e["slots"].append({"id": sid, "dow": dow, "active": active})

        with conn.cursor() as cur:
            for row in rows:
                ion_task_id = str(row.get("ionTaskId") or "").strip()
                if not ion_task_id:
                    continue
                if row.get("expired"):
                    stats["skipped_expired"] += 1
                    continue

                entry = sched.get(ion_task_id)
                if not entry:
                    stats["unmatched_iontask"] += 1
                    if len(stats["unmatched_examples"]) < 15:
                        stats["unmatched_examples"].append({
                            "ion_task_id": ion_task_id, "ion_cust_id": row.get("ionCustId"),
                            "recurrence": row.get("recurrence"),
                        })
                    continue

                desired = sorted(set(int(d) for d in (row.get("activeDays") or []) if d is not None))
                if not desired:
                    continue
                freq = _map_frequency(row.get("recurrence"), row.get("weekParity"))
                if freq:
                    stats["by_frequency"][freq] += 1
                tech_id = _resolve_tech(by_full, by_suffix, row.get("assignedTo"))
                if row.get("assignedTo") and "ASSIGN PEND" not in (row.get("assignedTo") or "").upper():
                    if tech_id is not None:
                        stats["tech_resolved"] += 1
                    else:
                        stats["tech_unresolved"] += 1
                        if len(stats["tech_unresolved_examples"]) < 15:
                            stats["tech_unresolved_examples"].append(row.get("assignedTo"))

                task_id = entry["task_id"]
                active_slots = [s for s in entry["slots"] if s["active"]]
                days_present = {s["dow"] for s in active_slots if s["dow"] is not None}
                dayless = [s for s in active_slots if s["dow"] is None]

                for day in desired:
                    if day in days_present:
                        cur.execute(
                            """UPDATE maintenance.task_schedules
                               SET tech_employee_id = COALESCE(%s, tech_employee_id),
                                   frequency = COALESCE(%s, frequency),
                                   active = true, external_source=%s, updated_at=now()
                               WHERE ion_task_id=%s AND day_of_week=%s""",
                            (tech_id, freq, source, ion_task_id, day),
                        )
                        stats["slots_updated"] += cur.rowcount
                    elif dayless:
                        s = dayless.pop(0)
                        cur.execute(
                            """UPDATE maintenance.task_schedules
                               SET day_of_week=%s,
                                   tech_employee_id = COALESCE(%s, tech_employee_id),
                                   frequency = COALESCE(%s, frequency),
                                   active = true, external_source=%s, updated_at=now()
                               WHERE id=%s""",
                            (day, tech_id, freq, source, s["id"]),
                        )
                        stats["slots_dayfilled"] += 1
                        days_present.add(day)
                    else:
                        cur.execute(
                            """INSERT INTO maintenance.task_schedules
                                 (task_id, ion_task_id, day_of_week, tech_employee_id,
                                  frequency, active, starts_on, external_source)
                               VALUES (%s, %s, %s, %s, %s, true, CURRENT_DATE, %s)""",
                            (task_id, ion_task_id, day, tech_id, freq, source),
                        )
                        stats["slots_inserted"] += 1
                        days_present.add(day)

                # leftover dayless slots for this task are surplus -> deactivate
                for s in dayless:
                    cur.execute(
                        """UPDATE maintenance.task_schedules SET active=false, updated_at=now()
                           WHERE id=%s""", (s["id"],))
                    stats["slots_deactivated"] += cur.rowcount

                if full_reconcile:
                    # deactivate dated slots whose day is no longer in the report
                    for s in active_slots:
                        if s["dow"] is not None and s["dow"] not in desired:
                            cur.execute(
                                """UPDATE maintenance.task_schedules SET active=false, updated_at=now()
                                   WHERE ion_task_id=%s AND day_of_week=%s""",
                                (ion_task_id, s["dow"]),
                            )
                            stats["slots_deactivated"] += cur.rowcount

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True

        stats["by_frequency"] = dict(stats["by_frequency"])
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(rows, supabase_connection, dry_run=True, full_reconcile=False, source="ion"):
    """Reconcile day_of_week/tech/frequency onto task_schedules from taskList rows.
    rows: list of {ionCustId, ionTaskId, activeDays[], recurrence, weekParity, assignedTo, expired}.
    dry_run default rolls back. full_reconcile also deactivates dated slots dropped from the report.
    """
    return sync_schedules(rows, supabase_connection, dry_run=dry_run, full_reconcile=full_reconcile, source=source)
