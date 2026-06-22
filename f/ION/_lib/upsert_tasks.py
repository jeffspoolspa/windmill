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
  RecurringtasksActive returns exactly ONE row per ION task. The stable ION task
  identity is `ionTaskId`. Matching is two-tier: the schedule map
  (task_schedules.ion_task_id, covers merged sub-ids) then the task's own 1:1 key
  (tasks.ion_task_id, covers schedule-less stubs — e.g. orphan-recovered 'ion_log'
  rows). A matched task with no slot for the id gets one minimal slot.

ONE TASK PER ion_task_id (new data model — 2026-06)
  The schema keys a task 1:1 by ion_task_id (`uq_tasks_ion_task_id`) and enforces
  one ACTIVE task per ion_task_id (`tasks_one_active_per_ion_task`). The
  one-open-per-location guard (`tasks_one_open_per_loc_manual`) applies ONLY to
  manual/native tasks (ion_task_id IS NULL) — so several ION tasks may share a
  service location. Every NEW ion_task_id becomes its OWN maintenance.tasks row
  (carrying ion_task_id + customer_id); we no longer bundle a second ION task as a
  schedule under an existing task (the legacy "merged" shape). The 15 pre-existing
  merged rows are left as-is (split separately via _lib/split_collapsed_tasks).

CLOSURE IS BY task_end — NOT report-absence (the stale-active fix — 2026-06)
  task_end (the ION end date) is the SOURCE OF TRUTH for closure, with one
  invariant: a task must have NO visits after its end date. So:
    - in-report task, task_end < today, no later visits -> CLOSED (ends_on = task_end).
      (e.g. WILLS, task_end 2026-05-20, last visit 2026-05-20 -> closed.)
    - in-report task, task_end < today, BUT visits after task_end -> the ION end
      date is stale; the task is still being serviced -> KEPT ACTIVE, ends_on cleared.
    - task absent from the report -> NOT a closure signal. The "Active Only" report
      omits actively-serviced commercial/POA/flat accounts (LOST PLANTATION is
      serviced ~24x/week yet absent), and visits are ground truth — so absence
      alone must never close a task. (This replaced the old close-on-absence sweep,
      which would have wrongly closed those accounts every run.)
  Genuine cancellations are caught while the task is still in the report with a
  past task_end. A fully-dropped, no-longer-visited task is left for a separate
  reviewed cancellation pass. See docs/operations/task-record-linkage.md.

CUSTOMER OWNER FROM ion_cust_id (ADR 006), NO LOCATION ON THE TASK (ADR 007 §9)
  tasks.customer_id is sourced from ION's customer id (ion_cust_id ->
  Customers.ion_cust_id), the authoritative per-task owner — not from the service
  location's account owner (the REGINA mis-attribution failure mode). A task carries
  NO service_location_id (the column is being dropped — a contract can outlive an
  address change; the visit's location is the customer's, resolved by
  public.reconcile_visit_locations). So there is no location-owner fallback: a new
  row whose ion_cust_id can't resolve to a Customer is left unresolved (full coverage
  today makes this rare).

MAPPING (report string -> column)
  billingType  -> billing_method: 'flat_rate_monthly' if 'FLAT' in upper else 'per_visit'
  serviceRepeat-> frequency:      Weekly->weekly, Bi-Weekly->biweekly_a (report
                  can't see the A/B split), Daily->daily, Monthly->monthly
  taskPrice    -> per_visit rows: price_per_visit_cents
                  flat rows:      flat_rate_monthly_cents
  taskStart/End-> tasks.starts_on / ends_on (subject to the no-visits-after-end invariant)

  FINANCIAL TERMS LIVE ON THE TASK (one ION contract = one rate). billing_method /
  price_per_visit_cents / flat_rate_monthly_cents are written to maintenance.tasks (the
  authoritative home). task_schedules carry ONLY routing — day_of_week, tech_employee_id,
  frequency, sequence; the slot financial columns were dropped once views + billing migrated to
  the task (migration 20260619150000).

SAFETY
  Defaults to dry_run=True: performs every INSERT/UPDATE inside one transaction,
  captures real rowcounts + examples, then ROLLS BACK. Set dry_run=False to commit.

Public API:
    sync_recurring_tasks(tasks, supabase_connection, dry_run=True, source='ion') -> stats
"""

from collections import defaultdict
from datetime import date as _date, datetime
import json
import re

# Reuse the proven resolver primitives (no re-port -> no drift vs visits sync).
from f.ION._lib.upsert import _connect  # sl-resolution removed (ADR 007 §9): a task carries customer_id, not a location


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
      sched_by_iontask  : {ion_task_id: {"task_id":.., "schedule_ids":[..]}}
      merged_task_ids   : set(task_id) that bundle >1 ion_task_id
      task_by_iontask   : {ion_task_id: task_id}  (the 1:1 key on maintenance.tasks)
      last_visit_by_task: {task_id: 'YYYY-MM-DD'} (max visit_date; the closure invariant)
      cust_by_ion_cust  : {ion_cust_id: Customers.id}  (authoritative task owner)
    """
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

    # task_id -> last visit date (ISO str). The closure invariant: a task with a
    # visit AFTER its ION task_end is still being serviced, so it must NOT close.
    last_visit_by_task = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT task_id, max(visit_date)::text
            FROM maintenance.visits
            WHERE task_id IS NOT NULL
            GROUP BY task_id
        """)
        for task_id, last_visit in cur.fetchall():
            last_visit_by_task[task_id] = last_visit

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
        "sched_by_iontask": sched_by_iontask,
        "merged_task_ids": merged_task_ids,
        "task_by_iontask": task_by_iontask,
        "last_visit_by_task": last_visit_by_task,
        "cust_by_ion_cust": cust_by_ion_cust,
    }


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
        "closed_by_end_date": 0,            # task_end past AND no later visits -> closed
        "kept_active_visits_after_end": 0,  # task_end past BUT later visits -> kept active
        "updated_tasks": 0,
        "updated_slots": 0,
        "new_tasks_inserted": 0,
        "new_slots_inserted": 0,
        "slots_created_for_existing": 0,    # matched task that had NO slot for this id
        "new_resolved_by": defaultdict(int),
        "new_task_examples": [],            # brand-new ION-task rows (1 per ion_task_id)
        "closed_examples": [],              # closed because their ION task_end is past
        "stale_end_examples": [],           # task_end past but later visits -> kept active
        "unresolved_new": 0,
        "unresolved_examples": [],
        "by_billing_method": defaultdict(int),
        "dry_run": dry_run,
        "committed": False,
    }
    try:
        r = _build_task_resolvers(conn)

        with conn.cursor() as cur:
            for row in tasks:
                ion_task_id = (row.get("ionTaskId") or "").strip()
                if not ion_task_id:
                    stats["skipped_no_iontask"] += 1
                    continue

                billing_method = map_billing_method(row.get("billingType"))
                freq = map_frequency(row.get("serviceRepeat"))
                price_cents = parse_price_cents(row.get("taskPrice"))
                starts_on = parse_ion_date(row.get("taskStart"))
                ends_on = parse_ion_date(row.get("taskEnd"))  # blank -> None (ongoing)
                stats["by_billing_method"][billing_method] += 1

                # Authoritative owner via ION's customer id (ADR 006), not loc owner.
                cust_id = r["cust_by_ion_cust"].get(str(row.get("ionCustId") or ""))

                ppv = price_cents if billing_method == "per_visit" else None
                flat = price_cents if billing_method == "flat_rate_monthly" else None

                # Match on the schedule map first (covers merged sub-ids), then fall
                # back to the task's own 1:1 ion_task_id (covers schedule-less stubs).
                existing = r["sched_by_iontask"].get(ion_task_id)
                task_id = existing["task_id"] if existing else r["task_by_iontask"].get(ion_task_id)
                has_sched = existing is not None

                # CLOSURE BY task_end, guarded by the invariant "no visits after the
                # end date". A past task_end with LATER visits means ION's end date is
                # stale and the task is still serviced -> keep active. ISO strings
                # compare correctly ('2026-05-20' < '2026-06-18').
                last_visit = r["last_visit_by_task"].get(task_id) if task_id is not None else None
                ended_by_date = ends_on is not None and ends_on < today
                is_ended = ended_by_date and (last_visit is None or last_visit <= ends_on)
                task_status = "closed" if is_ended else "active"
                slot_active = not is_ended

                # The ends_on we WRITE must never sit before the last visit:
                #   None / future task_end -> keep as-is (ongoing or legit future end)
                #   past task_end, no later visits -> the real end date (close)
                #   past task_end, later visits   -> stale -> NULL (treat as ongoing)
                if ends_on is None or ends_on >= today or is_ended:
                    write_ends_on = ends_on
                else:
                    write_ends_on = None

                if is_ended:
                    stats["closed_by_end_date"] += 1
                    if len(stats["closed_examples"]) < 60:
                        stats["closed_examples"].append({
                            "ion_task_id": ion_task_id,
                            "customer": row.get("customerName"),
                            "ended_on": ends_on, "last_visit": last_visit,
                        })
                elif ended_by_date:
                    stats["kept_active_visits_after_end"] += 1
                    if len(stats["stale_end_examples"]) < 60:
                        stats["stale_end_examples"].append({
                            "ion_task_id": ion_task_id,
                            "customer": row.get("customerName"),
                            "ion_task_end": ends_on, "last_visit": last_visit,
                        })

                if task_id is not None:
                    stats["matched_existing"] += 1

                    if task_id in r["merged_task_ids"]:
                        # A merged task bundles >1 ion_task_id; one report row can't
                        # own its status/dates/owner. Assert 'active' only from an
                        # active row; never close from a single ended sub-task (a
                        # fully-ended merged task is closed by the legacy split tool).
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
                                   billing_method=%s,
                                   price_per_visit_cents = CASE WHEN %s='per_visit'
                                        THEN %s ELSE price_per_visit_cents END,
                                   flat_rate_monthly_cents = CASE WHEN %s='flat_rate_monthly'
                                        THEN %s ELSE flat_rate_monthly_cents END,
                                   external_data=%s::jsonb,
                                   external_source=%s,
                                   updated_at=now()
                               WHERE id=%s""",
                            (task_status, starts_on, write_ends_on, cust_id, ion_task_id,
                             billing_method, billing_method, ppv, billing_method, flat,
                             json.dumps(_build_external_data(row)), source, task_id),
                        )
                    stats["updated_tasks"] += cur.rowcount

                    # Slots for this ion_task_id are ROUTING ONLY (day/tech/frequency +
                    # active/ends). Financial terms live on the task. frequency set only
                    # when currently NULL (don't clobber biweekly_a/_b).
                    cur.execute(
                        """UPDATE maintenance.task_schedules
                           SET frequency = COALESCE(frequency, %s),
                               active=%s,
                               ends_on=%s,
                               external_source=%s,
                               updated_at=now()
                           WHERE ion_task_id=%s""",
                        (freq, slot_active, write_ends_on, source, ion_task_id),
                    )
                    stats["updated_slots"] += cur.rowcount

                    # Matched task carries this ion_task_id but has NO schedule slot
                    # for it (an orphan-recovered 'ion_log' stub now surfacing in the
                    # active report) -> give it one minimal slot so routing + terms
                    # have a home (upsert_schedules fills day/tech later).
                    if not has_sched:
                        cur.execute(
                            """INSERT INTO maintenance.task_schedules
                                 (task_id, ion_task_id, frequency, active, starts_on, ends_on, external_source)
                               VALUES (%s, %s, %s, %s, COALESCE(%s, CURRENT_DATE), %s, %s)
                               RETURNING id""",
                            (task_id, ion_task_id, freq, slot_active, starts_on, write_ends_on, source),
                        )
                        new_sched_id = cur.fetchone()[0]
                        stats["slots_created_for_existing"] += 1
                        r["sched_by_iontask"][ion_task_id] = {
                            "task_id": task_id, "schedule_ids": [new_sched_id],
                        }

                else:
                    # New ION task. ADR 007 §9: a task carries customer_id, NOT a service location
                    # (the visit's location is the customer's, via reconcile_visit_locations). The
                    # authoritative owner is ION's customer id (ion_cust_id -> Customers, ADR 006);
                    # with no location there is no account_id fallback, so a task we can't attribute
                    # to a customer is left unresolved (full ion_cust_id coverage makes this rare).
                    if cust_id is None:
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

                    stats["new_resolved_by"]["ion_cust_id"] += 1

                    # New data model: each ION task is its OWN tasks row, keyed 1:1 by
                    # ion_task_id (uq_tasks_ion_task_id), carrying customer_id (the ION owner).
                    cur.execute(
                        """INSERT INTO maintenance.tasks
                             (ion_task_id, customer_id, status,
                              starts_on, ends_on, billing_method, price_per_visit_cents,
                              flat_rate_monthly_cents, external_source, external_data)
                           VALUES (%s, %s::bigint,
                                   %s, COALESCE(%s, CURRENT_DATE), %s, %s, %s, %s, %s, %s::jsonb)
                           RETURNING id""",
                        (ion_task_id, cust_id, task_status,
                         starts_on, write_ends_on, billing_method, ppv, flat,
                         source, json.dumps(_build_external_data(row))),
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
                            "resolved_by": "ion_cust_id",
                        })

                    cur.execute(
                        """INSERT INTO maintenance.task_schedules
                             (task_id, ion_task_id, frequency, active, starts_on, ends_on, external_source)
                           VALUES (%s, %s, %s, %s, COALESCE(%s, CURRENT_DATE), %s, %s)
                           RETURNING id""",
                        (new_task_id, ion_task_id, freq, slot_active, starts_on, write_ends_on, source),
                    )
                    new_sched_id = cur.fetchone()[0]
                    stats["new_slots_inserted"] += 1
                    # Register so a duplicate ion_task_id later in the run UPDATES
                    # instead of double-inserting (uq_tasks_ion_task_id would abort
                    # the whole transaction).
                    r["sched_by_iontask"][ion_task_id] = {
                        "task_id": new_task_id, "schedule_ids": [new_sched_id],
                    }

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
