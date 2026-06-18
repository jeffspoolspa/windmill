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
  that date has visits but NO task row — and a visit with no task can't be
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
  one task row (their slots legitimately disagree on price/frequency) — matching
  by ion_task_id only ever updates the right slots.

ONE TASK PER ion_task_id (new data model — 2026-06)
  The schema now keys a task 1:1 by ion_task_id (`uq_tasks_ion_task_id`, unique
  where ion_task_id IS NOT NULL) and enforces one ACTIVE task per ion_task_id
  (`tasks_one_active_per_ion_task`). The one-open-per-location guard
  (`tasks_one_open_per_loc_manual`) now applies ONLY to manual/native tasks
  (ion_task_id IS NULL) — so several ION tasks may legitimately share a service
  location. Therefore every NEW ion_task_id becomes its OWN maintenance.tasks row
  (carrying ion_task_id + customer_id); we no longer bundle a second ION task as a
  schedule under an existing task (the legacy "merged" shape). The 15 pre-existing
  merged rows are left as-is (split separately via _lib/split_collapsed_tasks);
  this sync updates the right slots by matching ion_task_id.

  Matching is two-tier: the schedule map (task_schedules.ion_task_id, covers
  merged sub-ids) then the task's own 1:1 key (tasks.ion_task_id, covers
  schedule-less stubs — e.g. orphan-recovered 'ion_log' rows). A matched task
  with no slot for the id gets one minimal slot.

STATUS FROM task_end (the stale-active fix — 2026-06)
  The "Active Only" report still carries recently-ENDED tasks (e.g. WILLS,
  task_end 2026-05-20, was in the feed yet expired). So status is derived from
  task_end (past => 'closed'), NEVER forced to 'active' off mere presence in the
  report. Without this, every sync re-opened ended tasks and inflated the
  app-wide "active customer" counts. See docs/operations/task-record-linkage.md.

CUSTOMER OWNER FROM ion_cust_id (ADR 006)
  tasks.customer_id is sourced from ION's customer id (ion_cust_id ->
  Customers.ion_cust_id), the authoritative per-task owner — not from the service
  location's account owner (the REGINA mis-attribution failure mode). Falls back
  to the location owner only when ion_cust_id can't resolve.

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
  - existing ion_task_id  -> update task (status from task_end, dates, owner,
                             external_data*) + update its slots (financial terms,
                             active/ends from task_end); create one slot if it had none
  - new ion_task_id       -> resolve service_location_id (ion_cust_id map, then
                             address+name, then address-only-if-unique); INSERT its
                             OWN task (ion_task_id + customer_id + status from
                             task_end) + one minimal schedule (no day/tech)
  - ion_task_id absent from the report -> cancellation. soft-deactivate:
                             slot.active=false; then task.status='closed' once it
                             has no active slot left. ends_on (slot + task) is
                             dated to the task's LAST VISIT (max maintenance.visits
                             .visit_date), per Carter -- a cancellation ends when
                             service last happened, not at sync time. (Mark closed,
                             never delete; 'closed' also frees the loc from
                             tasks_one_open_per_loc.)
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


# ─── value derivation ─────────────────────────────────────────────────────────

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


# ─── resolvers ────────────────────────────────────────────────────────────────

def _build_task_resolvers(conn):
    """
    Returns:
      sl_by_addr_name : {(norm_addr, norm_name): sl_id}
      sl_by_addr_only : {norm_addr: [sl_id, ...]}
      sl_by_ion_cust  : {ion_cust_id: sl_id}   (from existing tasks' external_data)
      sched_by_iontask: {ion_task_id: {"task_id":.., "schedule_ids":[..]}}
      merged_task_ids : set(task_id) that bundle >1 ion_task_id (don't refresh metadata)
      task_by_iontask : {ion_task_id: task_id}  (the 1:1 key on maintenance.tasks)
      cust_by_ion_cust: {ion_cust_id: Customers.id}  (authoritative task owner)
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

    # ion_task_id -> task_id straight off maintenance.tasks (the 1:1 key). Catches
    # tasks that carry an ion_task_id but have NO task_schedules row (e.g. the
    # orphan-recovered 'ion_log' stubs) — invisible to sched_by_iontask, so without
    # this they'd be mis-treated as NEW and collide on uq_tasks_ion_task_id.
    task_by_iontask = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ion_task_id, id FROM maintenance.tasks
            WHERE ion_task_id IS NOT NULL
        """)
        for ion_task_id, task_id in cur.fetchall():
            task_by_iontask[str(ion_task_id)] = task_id

    # ion_cust_id -> QBO Customers.id. The AUTHORITATIVE per-task owner (ADR 006):
    # tasks.customer_id is sourced from this, not from the service_location owner
    # (the REGINA mis-attribution fix). Full coverage today — every active-report
    # row resolves to a Customer via Customers.ion_cust_id.
    cust_by_ion_cust = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ion_cust_id, id FROM public."Customers"
            WHERE ion_cust_id IS NOT NULL
        """)
        for ion_cust_id, cust_id in cur.fetchall():
            cust_by_ion_cust.setdefault(str(ion_cust_id), cust_id)

    return {
        "sl_by_addr_name": sl_by_addr_name,
        "sl_by_addr_only": sl_by_addr_only,
        "sl_by_ion_cust": sl_by_ion_cust,
        "sched_by_iontask": sched_by_iontask,
        "merged_task_ids": merged_task_ids,
        "task_by_iontask": task_by_iontask,
        "cust_by_ion_cust": cust_by_ion_cust,
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


# ─── core sync ────────────────────────────────────────────────────────────────

def sync_recurring_tasks(tasks, supabase_connection, dry_run=True, source="ion"):
    conn = _connect(supabase_connection)
    today = _date.today().isoformat()
    stats = {
        "rows_total": len(tasks),
        "skipped_no_iontask": 0,
        "matched_existing": 0,
        "ended_in_report": 0,          # rows whose ION task_end is already past -> closed
        "updated_tasks": 0,
        "updated_slots": 0,
        "new_tasks_inserted": 0,
        "new_slots_inserted": 0,
        "slots_created_for_existing": 0,  # matched task that had NO slot for this id
        "new_resolved_by": defaultdict(int),
        "new_task_examples": [],       # brand-new ION-task rows (1 per ion_task_id)
        "closed_examples": [],         # tasks soft-closed (gone from report)
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

                # Status from ION's task_end, NOT from mere presence in the report.
                # The "Active Only" feed still carries recently-ENDED tasks, so a past
                # task_end => closed even while present. ISO date strings compare
                # correctly ('2026-05-20' < '2026-06-18').
                is_ended = ends_on is not None and ends_on < today
                task_status = "closed" if is_ended else "active"
                slot_active = not is_ended
                if is_ended:
                    stats["ended_in_report"] += 1

                # Authoritative owner via ION's customer id (ADR 006), not loc owner.
                cust_id = r["cust_by_ion_cust"].get(str(row.get("ionCustId") or ""))

                ppv = price_cents if billing_method == "per_visit" else None
                flat = price_cents if billing_method == "flat_rate_monthly" else None

                # Match on the schedule map first (covers merged sub-ids), then fall
                # back to the task's own 1:1 ion_task_id (covers schedule-less stubs).
                existing = r["sched_by_iontask"].get(ion_task_id)
                task_id = existing["task_id"] if existing else r["task_by_iontask"].get(ion_task_id)
                has_sched = existing is not None

                if task_id is not None:
                    stats["matched_existing"] += 1

                    if task_id in r["merged_task_ids"]:
                        # A merged task bundles >1 ion_task_id; one report row can't
                        # own its status/dates/owner. Assert 'active' only from an
                        # active row; NEVER close from a single ended sub-task — the
                        # end-of-run "no active slot -> closed" sweep closes it once
                        # ALL its slots go inactive.
                        if not is_ended:
                            cur.execute(
                                """UPDATE maintenance.tasks
                                   SET status='active', updated_at=now()
                                   WHERE id=%s AND status <> 'closed'""",
                                (task_id,),
                            )
                        else:
                            cur.execute(
                                "UPDATE maintenance.tasks SET updated_at=now() WHERE id=%s",
                                (task_id,),
                            )
                    else:
                        # 1 ion_task_id == 1 task (the norm): own status/dates/owner.
                        cur.execute(
                            """UPDATE maintenance.tasks
                               SET status=%s,
                                   starts_on=COALESCE(%s, starts_on),
                                   ends_on=%s,
                                   customer_id=COALESCE(%s::bigint, customer_id),
                                   ion_task_id=COALESCE(ion_task_id, %s),
                                   external_data=%s::jsonb,
                                   external_source=%s,
                                   updated_at=now()
                               WHERE id=%s""",
                            (task_status, starts_on, ends_on, cust_id, ion_task_id,
                             json.dumps(_build_external_data(row)), source, task_id),
                        )
                    stats["updated_tasks"] += cur.rowcount

                    # Slots for this ion_task_id: financial terms + active/ends from
                    # task_end. frequency set only when currently NULL (don't clobber
                    # biweekly_a/_b).
                    cur.execute(
                        """UPDATE maintenance.task_schedules
                           SET billing_method=%s,
                               price_per_visit_cents = CASE WHEN %s='per_visit'
                                    THEN %s ELSE price_per_visit_cents END,
                               flat_rate_monthly_cents = CASE WHEN %s='flat_rate_monthly'
                                    THEN %s ELSE flat_rate_monthly_cents END,
                               frequency = COALESCE(frequency, %s),
                               active=%s,
                               ends_on=%s,
                               external_source=%s,
                               updated_at=now()
                           WHERE ion_task_id=%s""",
                        (billing_method,
                         billing_method, ppv,
                         billing_method, flat,
                         freq, slot_active, ends_on, source, ion_task_id),
                    )
                    stats["updated_slots"] += cur.rowcount

                    # Matched task carries this ion_task_id but has NO schedule slot
                    # for it (an orphan-recovered 'ion_log' stub now surfacing in the
                    # active report) -> give it one minimal slot so routing + terms
                    # have a home (upsert_schedules fills day/tech later), and so the
                    # "no active slot -> closed" sweep doesn't immediately re-close it.
                    if not has_sched:
                        cur.execute(
                            """INSERT INTO maintenance.task_schedules
                                 (task_id, ion_task_id, frequency, billing_method,
                                  price_per_visit_cents, flat_rate_monthly_cents,
                                  active, starts_on, ends_on, external_source)
                               VALUES (%s, %s, %s, %s, %s, %s, %s,
                                       COALESCE(%s, CURRENT_DATE), %s, %s)
                               RETURNING id""",
                            (task_id, ion_task_id, freq, billing_method,
                             ppv, flat, slot_active, starts_on, ends_on, source),
                        )
                        new_sched_id = cur.fetchone()[0]
                        stats["slots_created_for_existing"] += 1
                        r["sched_by_iontask"][ion_task_id] = {
                            "task_id": task_id, "schedule_ids": [new_sched_id],
                        }

                else:
                    # New ION task — needs a service_location to live on.
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

                    # New data model: each ION task is its OWN tasks row, keyed 1:1 by
                    # ion_task_id (uq_tasks_ion_task_id). Several ION tasks may share a
                    # location — the one-open-per-loc guard now applies only to manual
                    # (ion_task_id IS NULL) tasks. customer_id is the ION owner
                    # (ion_cust_id -> Customers), falling back to the location owner.
                    cur.execute(
                        """INSERT INTO maintenance.tasks
                             (service_location_id, ion_task_id, customer_id, status,
                              starts_on, ends_on, external_source, external_data)
                           VALUES (%s, %s,
                                   COALESCE(%s::bigint,
                                     (SELECT account_id FROM public.service_locations WHERE id=%s)),
                                   %s, COALESCE(%s, CURRENT_DATE), %s, %s, %s::jsonb)
                           RETURNING id""",
                        (sl_id, ion_task_id, cust_id, sl_id, task_status,
                         starts_on, ends_on, source, json.dumps(_build_external_data(row))),
                    )
                    new_task_id = cur.fetchone()[0]
                    stats["new_tasks_inserted"] += 1
                    if len(stats["new_task_examples"]) < 60:
                        stats["new_task_examples"].append({
                            "ion_task_id": ion_task_id,
                            "customer": row.get("customerName"),
                            "address": row.get("serviceAddress"),
                            "city": row.get("city"),
                            "service_type": row.get("serviceType"),
                            "status": task_status,
                            "resolved_by": how,
                        })

                    cur.execute(
                        """INSERT INTO maintenance.task_schedules
                             (task_id, ion_task_id, frequency, billing_method,
                              price_per_visit_cents, flat_rate_monthly_cents,
                              active, starts_on, ends_on, external_source)
                           VALUES (%s, %s, %s, %s, %s, %s, %s,
                                   COALESCE(%s, CURRENT_DATE), %s, %s)
                           RETURNING id""",
                        (new_task_id, ion_task_id, freq, billing_method,
                         ppv, flat, slot_active, starts_on, ends_on, source),
                    )
                    new_sched_id = cur.fetchone()[0]
                    stats["new_slots_inserted"] += 1
                    # Register so a duplicate ion_task_id later in the run UPDATES
                    # instead of double-inserting (uq_tasks_ion_task_id would abort
                    # the whole transaction).
                    r["sched_by_iontask"][ion_task_id] = {
                        "task_id": new_task_id, "schedule_ids": [new_sched_id],
                    }

            # ── soft-deactivate everything ion-sourced that's gone from the report ──
            # Slots first.
            if report_ids:
                cur.execute(
                    """UPDATE maintenance.task_schedules ts
                       SET active=false,
                           ends_on=COALESCE(
                             (SELECT max(v.visit_date) FROM maintenance.visits v
                              WHERE v.task_id = ts.task_id),
                             ts.ends_on, %s::date),
                           updated_at=now()
                       WHERE ts.external_source=%s
                         AND ts.active=true
                         AND ts.ion_task_id IS NOT NULL
                         AND NOT (ts.ion_task_id = ANY(%s))""",
                    (today, source, report_ids),
                )
                stats["deactivated_slots"] = cur.rowcount

                # Tasks with no active slot left -> closed (allowed statuses:
                # active|paused|closed). 'closed' also exits tasks_one_open_per_loc.
                # CTE returns the closed rows joined to customer/address so the
                # dry-run can show exactly which tasks would close (no replacement
                # ion_task_id anywhere in the active report).
                cur.execute(
                    """WITH closed AS (
                           UPDATE maintenance.tasks t
                           SET status='closed',
                               ends_on=COALESCE(
                                 (SELECT max(v.visit_date) FROM maintenance.visits v
                                  WHERE v.task_id = t.id),
                                 t.ends_on),
                               updated_at=now()
                           WHERE t.external_source=%s
                             AND t.status <> 'closed'
                             AND NOT EXISTS (
                               SELECT 1 FROM maintenance.task_schedules ts
                               WHERE ts.task_id=t.id AND ts.active=true
                             )
                           RETURNING t.id, t.service_location_id,
                                     t.external_data->>'service_type' AS service_type,
                                     t.ends_on
                       )
                       SELECT cl.id::text, sl.street, c.display_name,
                              cl.service_type, cl.ends_on
                       FROM closed cl
                       JOIN public.service_locations sl ON sl.id = cl.service_location_id
                       JOIN public."Customers" c ON c.id = sl.account_id""",
                    (source,),
                )
                closed_rows = cur.fetchall()
                stats["deactivated_tasks"] = len(closed_rows)
                for _tid, street, display_name, service_type, end_dt in closed_rows[:60]:
                    stats["closed_examples"].append({
                        "customer": display_name,
                        "address": street,
                        "service_type": service_type,
                        "ended_on": end_dt.isoformat() if end_dt else None,
                    })

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


# ─── Windmill entry (flow step b) ───────────────────────────────────────────

def main(tasks, supabase_connection, dry_run=True, source="ion"):
    """Reconcile ION RecurringTask rows into maintenance.tasks/task_schedules.

    tasks: list of normalized RecurringTask dicts (from f/ION/api/list_recurring_tasks).
    Defaults to dry_run (rolls back). Pass dry_run=False to commit.
    """
    return sync_recurring_tasks(tasks, supabase_connection, dry_run=dry_run, source=source)
