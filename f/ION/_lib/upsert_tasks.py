# requirements:
# psycopg2-binary

"""
f/ION/_lib/upsert_tasks

Recurring-task sync: take normalized RecurringTask rows from the ION
RecurringtasksActive report (produced by f/ION/api/list_recurring_tasks, which
imports f/ION/_lib/reports.getRecurringTasks) and reconcile them into
maintenance.tasks + maintenance.task_schedules.

WHY THIS EXISTS
  All 469 maintenance.tasks were created by a one-time import on 2026-04-26 and
  never refreshed. Visits sync continuously, so every customer onboarded after
  that date has visits but NO task row -- and a visit with no task can't be
  billed. This sync makes the ION active-tasks report the ongoing leader for
  task existence + financial terms, the analog of ion-visits / ion-work-orders.

THE KEY: ion_task_id
  RecurringtasksActive returns exactly ONE row per ION task (487 rows, 487
  distinct ionTaskId; the report's `seq` column is empty). The stable ION task
  identity is `ionTaskId`, stored on maintenance.task_schedules.ion_task_id.
  Verified in the DB: every ion_task_id maps to exactly one maintenance.task
  (0 span >1 task), so ion_task_id is a safe upsert key.

WHAT THE REPORT CANNOT SUPPLY (so this sync never touches it on existing rows)
  The report has no day-of-week and no tech column. The per-day SLOT structure
  (96 tasks have >1 task_schedules row; e.g. task 3369746 = Mon+Fri) and the
  bi-weekly A/B alternation came from the richer 2026-04-26 import, not this
  report. So for EXISTING slots we update only financial terms (price, billing
  method, active, ends_on) filtered BY ion_task_id, and leave day_of_week,
  tech_employee_id, sequence, and an already-set frequency untouched. This also
  sidesteps the 15 legacy "merged" tasks that bundle multiple ion_task_ids under
  one task row (their slots legitimately disagree on price/frequency) -- matching
  by ion_task_id only ever updates the right slots.

MULTI-TASK LOCATIONS (tasks_one_open_per_loc)
  A partial unique index allows only ONE open task (status active|paused) per
  service_location. So a NEW ion_task_id whose location already has an open task
  is a second contract at the same place -> we attach it as another schedule on
  that existing task (the "merged" shape) rather than insert a second task.

MAPPING (report string -> column)
  billingType  -> billing_method: 'flat_rate_monthly' if 'FLAT' in upper else 'per_visit'
                  ("Flat Rate (list consumables)" is the only flat variant; the
                   4 per-visit variants + "Do Not Invoice" -> per_visit)
  serviceRepeat-> frequency:      Weekly->weekly, Bi-Weekly->biweekly_a (report
                  can't see the A/B split), Daily->daily, Monthly->monthly
  taskPrice    -> per_visit rows: price_per_visit_cents
                  flat rows:      flat_rate_monthly_cents (report's flat price IS
                  the monthly amount; per-visit column left as-is on flat tasks)
  taskStart/End-> tasks.starts_on / ends_on (+ schedule ends_on on deactivation)

LIFECYCLE
  - existing ion_task_id  -> update task (status='active', dates, external_data*)
                             + update its slots' financial terms
  - new ion_task_id       -> resolve service_location_id; attach to the loc's open
                             task if one exists, else INSERT task + one minimal
                             schedule (no day/tech)
  - ion_task_id absent from the report -> soft-deactivate: slot.active=false,
                             ends_on; then task.status='inactive' once it has no
                             active slot left. (Carter: mark inactive, never delete.)
  (*external_data/starts_on refreshed only for un-merged tasks; merged tasks get
    status/updated_at only, since one report row can't own a merged row's metadata.)

SAFETY
  Defaults to dry_run=True: performs every INSERT/UPDATE inside one transaction,
  captures real rowcounts, then ROLLS BACK. Set dry_run=False to commit.

Public API:
    sync_recurring_tasks(tasks, supabase_connection, dry_run=True, source='ion') -> stats
"""

from collections import defaultdict
from datetime import date as _date, datetime
import json
import re

# Reuse the proven resolver primitives (no re-port -> no drift vs visits sync).
from f.ION._lib.upsert import _connect, normalize_address, normalize_customer_name


# --- value derivation --------------------------------------------------------

def parse_price_cents(s):
    """'$1,550.00' -> 155000; '' / '$0.00' -> 0; None -> 0."""
    if not s:
        return 0
    cleaned = re.sub(r"[^0-9.]", "", str(s))
    if not cleaned:
        return 0
    try:
        return int(round(float(cleaned) * 100))
    except (ValueError, TypeError):
        return 0


def map_billing_method(billing_type):
    """ION billingType string -> our billing_method. Mirrors upsert.derive_billing_method."""
    if not billing_type:
        return "per_visit"
    return "flat_rate_monthly" if "FLAT" in billing_type.upper() else "per_visit"


_FREQ_MAP = {
    "WEEKLY": "weekly",
    "BI-WEEKLY": "biweekly_a",
    "BIWEEKLY": "biweekly_a",
    "DAILY": "daily",
    "MONTHLY": "monthly",
}


def map_frequency(service_repeat):
    if not service_repeat:
        return None
    return _FREQ_MAP.get(service_repeat.upper().strip())


def parse_ion_date(s):
    """'04/15/2022' -> '2022-04-15' (isoformat str); '' -> None."""
    if not s or not str(s).strip():
        return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --- resolvers ---------------------------------------------------------------

def _build_task_resolvers(conn):
    """
    Returns:
      sl_by_addr_name  : {(norm_addr, norm_name): sl_id}
      sl_by_addr_only  : {norm_addr: [sl_id, ...]}
      sl_by_ion_cust   : {ion_cust_id: sl_id}   (from existing tasks' external_data)
      sched_by_iontask : {ion_task_id: {"task_id":.., "schedule_ids":[..]}}
      merged_task_ids  : set(task_id) that bundle >1 ion_task_id (don't refresh metadata)
      open_task_by_loc : {service_location_id: open task_id}
    """
    sl_by_addr_name = {}
    sl_by_addr_only = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sl.id, sl.street, c.display_name
            FROM public.service_locations sl
            JOIN public."Customers" c ON c.id = sl.account_id
            WHERE sl.is_active
        """)
        for sl_id, street, display_name in cur.fetchall():
            n_addr = normalize_address(street or "")
            if not n_addr:
                continue
            n_name = normalize_customer_name(display_name or "")
            sl_by_addr_name[(n_addr, n_name)] = sl_id
            sl_by_addr_only[n_addr].append(sl_id)

    # ion_cust_id -> service_location_id, learned from existing ion tasks.
    sl_by_ion_cust = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT external_data->>'ion_cust_id' AS ion_cust_id, service_location_id
            FROM maintenance.tasks
            WHERE external_source = 'ion'
              AND external_data->>'ion_cust_id' IS NOT NULL
        """)
        for ion_cust_id, sl_id in cur.fetchall():
            # First wins; multi-task customers share a service_location anyway.
            sl_by_ion_cust.setdefault(ion_cust_id, sl_id)

    # ion_task_id -> {task_id, schedule_ids[]}  +  detect merged tasks.
    sched_by_iontask = {}
    iontasks_per_task = defaultdict(set)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ion_task_id, id, task_id
            FROM maintenance.task_schedules
            WHERE ion_task_id IS NOT NULL
        """)
        for ion_task_id, sched_id, task_id in cur.fetchall():
            entry = sched_by_iontask.setdefault(
                ion_task_id, {"task_id": task_id, "schedule_ids": []}
            )
            entry["schedule_ids"].append(sched_id)
            iontasks_per_task[task_id].add(ion_task_id)

    merged_task_ids = {t for t, ids in iontasks_per_task.items() if len(ids) > 1}

    # service_location_id -> open task_id. The partial unique index
    # tasks_one_open_per_loc allows only ONE task with status in (active,paused)
    # per location, so a NEW ion_task_id whose location already has an open task
    # must attach as another SCHEDULE on that task, not insert a second task.
    open_task_by_loc = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT service_location_id, id FROM maintenance.tasks
            WHERE status IN ('active','paused')
        """)
        for sl_id, task_id in cur.fetchall():
            open_task_by_loc.setdefault(sl_id, task_id)

    return {
        "sl_by_addr_name": sl_by_addr_name,
        "sl_by_addr_only": sl_by_addr_only,
        "sl_by_ion_cust": sl_by_ion_cust,
        "sched_by_iontask": sched_by_iontask,
        "merged_task_ids": merged_task_ids,
        "open_task_by_loc": open_task_by_loc,
    }


def _resolve_sl(resolvers, ion_cust_id, addr, name):
    # 1) known ION customer -> its service_location
    sl = resolvers["sl_by_ion_cust"].get(ion_cust_id)
    if sl is not None:
        return sl, "ion_cust_id"
    # 2) address + name
    n_addr = normalize_address(addr or "")
    if n_addr:
        n_name = normalize_customer_name(name or "")
        if (n_addr, n_name) in resolvers["sl_by_addr_name"]:
            return resolvers["sl_by_addr_name"][(n_addr, n_name)], "addr_name"
        # 3) address-only if unique
        cands = resolvers["sl_by_addr_only"].get(n_addr, [])
        if len(cands) == 1:
            return cands[0], "addr_only"
    return None, None


def _build_external_data(row, slot_count=1):
    return {
        "ion_cust_id": row.get("ionCustId") or "",
        "service_type": row.get("serviceType") or "",
        "billing_type": row.get("billingType") or "",
        "customer_type": row.get("customerType") or "",
        "zone": row.get("zone") or "",
        "service_profile": row.get("serviceProfile") or "",
        "facility_description": row.get("facilityDescription") or "",
        "lock_combo": row.get("lockCombo") or "",
        "route_name": row.get("routeName") or "",
        "recurring_notes": row.get("recurringNotes") or "",
        "slot_count": slot_count,
    }


# --- core sync ---------------------------------------------------------------

def sync_recurring_tasks(tasks, supabase_connection, dry_run=True, source="ion"):
    conn = _connect(supabase_connection)
    today = _date.today().isoformat()
    stats = {
        "rows_total": len(tasks),
        "skipped_no_iontask": 0,
        "matched_existing": 0,
        "updated_tasks": 0,
        "updated_slots": 0,
        "new_tasks_inserted": 0,
        "new_slots_inserted": 0,
        "attached_slots_to_existing_task": 0,
        "new_resolved_by": defaultdict(int),
        "unresolved_new": 0,
        "unresolved_examples": [],
        "deactivated_slots": 0,
        "deactivated_tasks": 0,
        "by_billing_method": defaultdict(int),
        "dry_run": dry_run,
        "committed": False,
    }
    try:
        r = _build_task_resolvers(conn)
        report_ids = []

        with conn.cursor() as cur:
            for row in tasks:
                ion_task_id = (row.get("ionTaskId") or "").strip()
                if not ion_task_id:
                    stats["skipped_no_iontask"] += 1
                    continue
                report_ids.append(ion_task_id)

                billing_method = map_billing_method(row.get("billingType"))
                freq = map_frequency(row.get("serviceRepeat"))
                price_cents = parse_price_cents(row.get("taskPrice"))
                starts_on = parse_ion_date(row.get("taskStart"))
                ends_on = parse_ion_date(row.get("taskEnd"))  # blank -> None (ongoing)
                stats["by_billing_method"][billing_method] += 1

                ppv = price_cents if billing_method == "per_visit" else None
                flat = price_cents if billing_method == "flat_rate_monthly" else None

                existing = r["sched_by_iontask"].get(ion_task_id)

                if existing:
                    stats["matched_existing"] += 1
                    task_id = existing["task_id"]

                    # Task row: always reactivate + bump; refresh metadata only
                    # for un-merged tasks (a merged row can't be owned by one report row).
                    if task_id in r["merged_task_ids"]:
                        cur.execute(
                            """UPDATE maintenance.tasks
                               SET status='active', ends_on=%s, updated_at=now()
                               WHERE id=%s""",
                            (ends_on, task_id),
                        )
                    else:
                        cur.execute(
                            """UPDATE maintenance.tasks
                               SET status='active',
                                   starts_on=COALESCE(%s, starts_on),
                                   ends_on=%s,
                                   external_data=%s::jsonb,
                                   external_source=%s,
                                   updated_at=now()
                               WHERE id=%s""",
                            (starts_on, ends_on,
                             json.dumps(_build_external_data(row)), source, task_id),
                        )
                    stats["updated_tasks"] += cur.rowcount

                    # Slots for this ion_task_id: financial terms only.
                    # frequency set only when currently NULL (don't clobber biweekly_a/_b).
                    cur.execute(
                        """UPDATE maintenance.task_schedules
                           SET billing_method=%s,
                               price_per_visit_cents = CASE WHEN %s='per_visit'
                                    THEN %s ELSE price_per_visit_cents END,
                               flat_rate_monthly_cents = CASE WHEN %s='flat_rate_monthly'
                                    THEN %s ELSE flat_rate_monthly_cents END,
                               frequency = COALESCE(frequency, %s),
                               active=true,
                               ends_on=%s,
                               external_source=%s,
                               updated_at=now()
                           WHERE ion_task_id=%s""",
                        (billing_method,
                         billing_method, ppv,
                         billing_method, flat,
                         freq, ends_on, source, ion_task_id),
                    )
                    stats["updated_slots"] += cur.rowcount

                else:
                    # New ION task -- needs a service_location to live on.
                    sl_id, how = _resolve_sl(
                        r, row.get("ionCustId"),
                        row.get("serviceAddress"), row.get("customerName"),
                    )
                    if sl_id is None:
                        stats["unresolved_new"] += 1
                        if len(stats["unresolved_examples"]) < 15:
                            stats["unresolved_examples"].append({
                                "ion_task_id": ion_task_id,
                                "customer": row.get("customerName"),
                                "address": row.get("serviceAddress"),
                                "city": row.get("city"),
                                "ion_cust_id": row.get("ionCustId"),
                            })
                        continue

                    stats["new_resolved_by"][how] += 1

                    # tasks_one_open_per_loc: only ONE open task per location.
                    # If this location already has an open task, this ION task is
                    # a second contract at the same place -> attach it as another
                    # schedule on that task (the "merged" multi-task-location shape)
                    # rather than inserting a second (constraint-violating) task.
                    target_task_id = r["open_task_by_loc"].get(sl_id)
                    if target_task_id is not None:
                        cur.execute(
                            """INSERT INTO maintenance.task_schedules
                                 (task_id, ion_task_id, frequency, billing_method,
                                  price_per_visit_cents, flat_rate_monthly_cents,
                                  active, starts_on, ends_on, external_source)
                               VALUES (%s, %s, %s, %s, %s, %s, true,
                                       COALESCE(%s, CURRENT_DATE), %s, %s)""",
                            (target_task_id, ion_task_id, freq, billing_method,
                             ppv, flat, starts_on, ends_on, source),
                        )
                        stats["attached_slots_to_existing_task"] += 1
                    else:
                        cur.execute(
                            """INSERT INTO maintenance.tasks
                                 (service_location_id, status, starts_on, ends_on,
                                  external_source, external_data)
                               VALUES (%s, 'active', COALESCE(%s, CURRENT_DATE), %s, %s, %s::jsonb)
                               RETURNING id""",
                            (sl_id, starts_on, ends_on, source,
                             json.dumps(_build_external_data(row))),
                        )
                        new_task_id = cur.fetchone()[0]
                        # Register so a SECOND new ion_task_id at this same (new)
                        # location later in the run attaches instead of double-inserting.
                        r["open_task_by_loc"][sl_id] = new_task_id
                        stats["new_tasks_inserted"] += 1

                        cur.execute(
                            """INSERT INTO maintenance.task_schedules
                                 (task_id, ion_task_id, frequency, billing_method,
                                  price_per_visit_cents, flat_rate_monthly_cents,
                                  active, starts_on, ends_on, external_source)
                               VALUES (%s, %s, %s, %s, %s, %s, true,
                                       COALESCE(%s, CURRENT_DATE), %s, %s)""",
                            (new_task_id, ion_task_id, freq, billing_method,
                             ppv, flat, starts_on, ends_on, source),
                        )
                        stats["new_slots_inserted"] += 1

            # -- soft-deactivate everything ion-sourced that's gone from the report --
            # Slots first.
            if report_ids:
                cur.execute(
                    """UPDATE maintenance.task_schedules
                       SET active=false,
                           ends_on=COALESCE(ends_on, %s::date),
                           updated_at=now()
                       WHERE external_source=%s
                         AND active=true
                         AND ion_task_id IS NOT NULL
                         AND NOT (ion_task_id = ANY(%s))""",
                    (today, source, report_ids),
                )
                stats["deactivated_slots"] = cur.rowcount

                # Tasks with no active slot left -> inactive.
                cur.execute(
                    """UPDATE maintenance.tasks t
                       SET status='inactive', updated_at=now()
                       WHERE t.external_source=%s
                         AND t.status <> 'inactive'
                         AND NOT EXISTS (
                           SELECT 1 FROM maintenance.task_schedules ts
                           WHERE ts.task_id=t.id AND ts.active=true
                         )""",
                    (source,),
                )
                stats["deactivated_tasks"] = cur.rowcount

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True

        stats["new_resolved_by"] = dict(stats["new_resolved_by"])
        stats["by_billing_method"] = dict(stats["by_billing_method"])
        return stats

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Windmill entry (flow step b) -------------------------------------------

def main(tasks, supabase_connection, dry_run=True, source="ion"):
    """Reconcile ION RecurringTask rows into maintenance.tasks/task_schedules.

    tasks: list of normalized RecurringTask dicts (from f/ION/api/list_recurring_tasks).
    Defaults to dry_run (rolls back). Pass dry_run=False to commit.
    """
    return sync_recurring_tasks(tasks, supabase_connection, dry_run=dry_run, source=source)
