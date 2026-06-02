# requirements:
# psycopg2-binary

"""
f/ION/_lib/upsert

Layer 3 of the ION ingest pipeline:
    parser   ->  raw ION dicts
    normalize ->  canonical-shaped dicts (this is the input)
    UPSERT   ->  maintenance.visits / chem_readings / consumables_usage / visit_tasks
                  + auto-create public.pools

Visit grain = (service_location_id, scheduled_date, service_type): ONE billable
visit per service per day. Multiple service bodies (pools) serviced under the
same service on the same day bill together as one visit (they collapse onto one
visit_id; per-pool chem/consumables/visit_tasks still attach via pool_id).
Different services (POOL MAINTENANCE vs CHEMICAL TESTING) on the same day stay
separate billable visits. Each visit attributes to the location task whose
schedule RATE matches the visit price (multi-contract communities).

Public API:
    upsert_canonical(canonical_rows, supabase_connection) -> stats dict
"""

import json
import re
from collections import defaultdict
from datetime import date as _date, datetime, time as _time, timezone

import psycopg2
import psycopg2.extras


# ─── connection ───────────────────────────────────────────────────────────────

def _connect(sb):
    return psycopg2.connect(
        host=sb["host"], port=sb["port"], dbname=sb["dbname"],
        user=sb["user"], password=sb["password"], connect_timeout=15,
    )


# ─── normalization (matches the TS one-shot's logic) ─────────────────────────

_STREET_SUFFIX = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "ROAD": "RD", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "CIRCLE": "CIR", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
    "SQUARE": "SQ", "PLAZA": "PLZ", "TRAIL": "TRL",
    "EXPRESSWAY": "EXPY", "CROSSING": "XING", "POINT": "PT",
    "RIDGE": "RDG", "HARBOR": "HBR", "ISLAND": "IS",
}
_DIRECTIONAL = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
_UNIT_REMOVABLES = {"APT", "APARTMENT", "UNIT", "SUITE", "STE", "BLDG", "BUILDING"}


def normalize_address(s):
    if not s:
        return ""
    t = s.upper()
    t = re.sub(r"[.,#]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    tokens = t.split(" ")
    out, skip_next = [], False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _UNIT_REMOVABLES:
            skip_next = True
            continue
        tok = _DIRECTIONAL.get(tok, tok)
        tok = _STREET_SUFFIX.get(tok, tok)
        out.append(tok)
    return " ".join(out)


def normalize_customer_name(s):
    if not s:
        return ""
    t = s.upper()
    t = re.sub(r"^\*+", "", t)
    t = re.sub(r"[.,'`’\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(sorted(t.split(" ")))


def normalize_tech_name(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.upper().replace(",", " ")).strip()


def expand_ion_username_variants(stored):
    out = set()
    t = (stored or "").strip()
    if not t:
        return []
    out.add(t)
    if "," in t:
        last, _, first = t.partition(",")
        last, first = last.strip(), first.strip()
        if last and first:
            out.add(f"{first} {last}")
            out.add(f"{last} {first}")
    return list(out)


# ─── resolvers (load once at start of run) ───────────────────────────────────

def build_resolvers(conn):
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

    tech_by_username = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, ion_username FROM public.employees WHERE ion_username IS NOT NULL")
        for emp_id, usernames in cur.fetchall():
            for u in (usernames or []):
                for variant in expand_ion_username_variants(u):
                    norm = normalize_tech_name(variant)
                    if norm and norm not in tech_by_username:
                        tech_by_username[norm] = emp_id

    items_by_name = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, item_name FROM public.items WHERE item_name IS NOT NULL")
        for item_id, item_name in cur.fetchall():
            items_by_name[item_name.upper().strip()] = item_id

    # Active tasks per service_location, WITH rate/billing for rate-based
    # attribution at multi-contract locations.
    tasks_by_sl = defaultdict(list)
    task_meta = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.service_location_id,
                   ts.id, ts.day_of_week, ts.tech_employee_id,
                   ts.billing_method, ts.price_per_visit_cents, ts.flat_rate_monthly_cents
            FROM maintenance.tasks t
            LEFT JOIN maintenance.task_schedules ts ON ts.task_id = t.id AND ts.active = true
            WHERE t.status IN ('active','paused')
        """)
        for task_id, sl_id, sched_id, dow, tech_id, bm, rate, flat in cur.fetchall():
            m = task_meta.get(task_id)
            if m is None:
                m = {"task_id": task_id, "rate": None, "flat": None,
                     "billing_method": None, "schedules": []}
                task_meta[task_id] = m
                tasks_by_sl[sl_id].append(m)
            if rate is not None and (m["rate"] is None or rate > m["rate"]):
                m["rate"] = rate
            if flat is not None and m["flat"] is None:
                m["flat"] = flat
            if bm and not m["billing_method"]:
                m["billing_method"] = bm
            if sched_id is not None:
                m["schedules"].append((sched_id, dow, tech_id))

    return {
        "sl_by_addr_name": sl_by_addr_name,
        "sl_by_addr_only": sl_by_addr_only,
        "tech_by_username": tech_by_username,
        "items_by_name": items_by_name,
        "tasks_by_sl": dict(tasks_by_sl),
    }


def _choose_task(candidates, price_cents, billing_method):
    """Pick which of a location's active tasks a visit belongs to (rate match)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if billing_method == "flat_rate_monthly":
        flats = [c for c in candidates if c["billing_method"] == "flat_rate_monthly"]
        return flats[0] if flats else candidates[0]
    pool = [c for c in candidates if c["billing_method"] != "flat_rate_monthly"] or candidates
    exact = [c for c in pool if c["rate"] is not None and c["rate"] == price_cents]
    if exact:
        return exact[0]
    return min(pool, key=lambda c: abs((c["rate"] or 0) - (price_cents or 0)))


def resolve_task_and_schedule(resolvers, service_location_id, visit_date, actual_tech_id,
                              price_cents=None, billing_method=None):
    """Returns (task_id, task_schedule_id, scheduled_tech_id) or all None if no task."""
    candidates = resolvers["tasks_by_sl"].get(service_location_id, [])
    chosen = _choose_task(candidates, price_cents, billing_method)
    if chosen is None:
        return None, None, None
    task_id = chosen["task_id"]

    schedules = chosen["schedules"]
    if not schedules:
        return task_id, None, None

    if hasattr(visit_date, "weekday"):
        py_dow = visit_date.weekday()
        pg_dow = (py_dow + 1) % 7
    else:
        return task_id, None, None

    for sched_id, dow, tech_id in schedules:
        if dow == pg_dow and tech_id == actual_tech_id:
            return task_id, sched_id, tech_id
    for sched_id, dow, tech_id in schedules:
        if dow == pg_dow:
            return task_id, sched_id, tech_id
    return task_id, None, None


def resolve_service_location_id(resolvers, addr, name):
    n_addr = normalize_address(addr or "")
    if not n_addr:
        return None
    n_name = normalize_customer_name(name or "")
    if (n_addr, n_name) in resolvers["sl_by_addr_name"]:
        return resolvers["sl_by_addr_name"][(n_addr, n_name)]
    candidates = resolvers["sl_by_addr_only"].get(n_addr, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_tech_id(resolvers, username):
    if not username:
        return None
    return resolvers["tech_by_username"].get(normalize_tech_name(username))


# ─── value derivation ─────────────────────────────────────────────────────────

def combine_datetime(d, time_str):
    if not d or not time_str:
        return None
    if isinstance(d, str):
        try:
            d = _date.fromisoformat(d)
        except ValueError:
            return None
    try:
        t = datetime.strptime(time_str.strip(), "%I:%M %p").time()
        return datetime.combine(d, t).isoformat()
    except (ValueError, TypeError):
        return None


def derive_visit_type(service_type, price_cents):
    if price_cents == 0:
        return "qc"
    if not service_type:
        return "route"
    upper = service_type.upper()
    if "QUALITY" in upper or "QC" in upper:
        return "qc"
    if "REPAIR" in upper:
        return "repair"
    if "SERVICE CALL" in upper or "SVC CALL" in upper:
        return "service_call"
    return "route"


def derive_billing_method(invoice_type):
    if not invoice_type:
        return "per_visit"
    return "flat_rate_monthly" if "FLAT" in invoice_type.upper() else "per_visit"


# ─── pool auto-create ─────────────────────────────────────────────────────────

def get_or_create_pool(conn, service_location_id, pool_name, source="ion"):
    if not pool_name or not service_location_id:
        return None, False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM public.pools WHERE service_location_id = %s AND name = %s LIMIT 1",
            (service_location_id, pool_name),
        )
        row = cur.fetchone()
        if row:
            return row[0], False
        cur.execute(
            """INSERT INTO public.pools (service_location_id, name, external_source, active)
               VALUES (%s, %s, %s, true) RETURNING id""",
            (service_location_id, pool_name, source),
        )
        return cur.fetchone()[0], True


# ─── core upsert ──────────────────────────────────────────────────────────────

def upsert_canonical(canonical_rows, supabase_connection, source="ion"):
    """Write canonical rows to maintenance.*; visits keyed (loc, date, service_type)."""
    conn = _connect(supabase_connection)
    try:
        resolvers = build_resolvers(conn)

        stats = {
            "rows_total": len(canonical_rows),
            "rows_resolved": 0,
            "rows_unresolved_sl": 0,
            "rows_unresolved_examples": [],
            "visits_upserted": 0,
            "pools_created": 0,
            "chem_readings_inserted": 0,
            "consumables_inserted": 0,
            "consumables_unresolved_items": defaultdict(int),
            "visit_tasks_inserted": 0,
        }

        visit_buffer = []
        per_row_extras = []

        for row in canonical_rows:
            v = row.get("visits", {}) or {}
            chem = row.get("chem_readings", {}) or {}
            pool_meta = row.get("pools", {}) or {}
            consumables = row.get("consumables_usage_rows", []) or []
            visit_tasks = row.get("visit_tasks_rows", []) or []

            primary_addr = v.get("_address2") or v.get("_address1")
            secondary_addr = v.get("_address1") if primary_addr == v.get("_address2") else None

            sl_id = resolve_service_location_id(
                resolvers, primary_addr, v.get("_customer_name")
            )
            if sl_id is None and secondary_addr:
                sl_id = resolve_service_location_id(
                    resolvers, secondary_addr, v.get("_customer_name")
                )
            if sl_id is None:
                stats["rows_unresolved_sl"] += 1
                if len(stats["rows_unresolved_examples"]) < 10:
                    stats["rows_unresolved_examples"].append({
                        "customer": v.get("_customer_name"),
                        "address1": v.get("_address1"),
                        "address2": v.get("_address2"),
                        "city": v.get("_city"),
                    })
                continue

            tech_id = resolve_tech_id(resolvers, v.get("_tech_username"))
            visit_date = v.get("visit_date")
            if not visit_date:
                continue

            started_at = combine_datetime(visit_date, v.get("_start_time_str"))
            ended_at = combine_datetime(visit_date, v.get("_end_time_str"))
            price_cents = v.get("price_cents") or 0
            visit_type = derive_visit_type(v.get("_service_type"), price_cents)
            billing_method = derive_billing_method(v.get("_invoice_type"))

            pool_name = pool_meta.get("_pool_name")
            pool_id, was_created = get_or_create_pool(conn, sl_id, pool_name, source=source)
            if was_created:
                stats["pools_created"] += 1

            task_id, task_schedule_id, scheduled_tech = resolve_task_and_schedule(
                resolvers, sl_id, visit_date, tech_id, price_cents, billing_method
            )
            if task_id is not None:
                stats.setdefault("tasks_linked", 0)
                stats["tasks_linked"] += 1
            if task_schedule_id is not None:
                stats.setdefault("schedules_linked", 0)
                stats["schedules_linked"] += 1
            if scheduled_tech and tech_id and scheduled_tech != tech_id:
                stats.setdefault("reassignments_detected", 0)
                stats["reassignments_detected"] += 1

            visit_buffer.append({
                "service_location_id": sl_id,
                "pool_id": pool_id,
                "service_type": v.get("_service_type"),
                "task_id": task_id,
                "task_schedule_id": task_schedule_id,
                "scheduled_date": visit_date,
                "visit_date": visit_date,
                "scheduled_tech_id": scheduled_tech,
                "actual_tech_id": tech_id,
                "started_at": started_at,
                "ended_at": ended_at,
                "status": "completed",
                "visit_type": visit_type,
                "price_cents": price_cents,
                "billing_method": billing_method,
                "office": v.get("office"),
                "notes": v.get("notes"),
                "external_source": source,
            })
            per_row_extras.append({
                "pool_id": pool_id,
                "chem": chem,
                "consumables": consumables,
                "visit_tasks": visit_tasks,
            })
            stats["rows_resolved"] += 1

        if not visit_buffer:
            conn.commit()
            return stats

        visit_ids = []
        with conn.cursor() as cur:
            for v in visit_buffer:
                cur.execute(
                    """
                    INSERT INTO maintenance.visits
                      (service_location_id, pool_id, service_type, task_id, task_schedule_id,
                       scheduled_date, visit_date,
                       scheduled_tech_id, actual_tech_id, started_at, ended_at,
                       status, visit_type, price_cents, billing_method,
                       office, notes, external_source)
                    VALUES
                      (%(service_location_id)s, %(pool_id)s, %(service_type)s, %(task_id)s, %(task_schedule_id)s,
                       %(scheduled_date)s, %(visit_date)s,
                       %(scheduled_tech_id)s, %(actual_tech_id)s, %(started_at)s, %(ended_at)s,
                       %(status)s, %(visit_type)s, %(price_cents)s, %(billing_method)s,
                       %(office)s, %(notes)s, %(external_source)s)
                    ON CONFLICT (service_location_id, scheduled_date, service_type) DO UPDATE SET
                      task_id             = COALESCE(EXCLUDED.task_id, maintenance.visits.task_id),
                      task_schedule_id    = COALESCE(EXCLUDED.task_schedule_id, maintenance.visits.task_schedule_id),
                      pool_id             = COALESCE(maintenance.visits.pool_id, EXCLUDED.pool_id),
                      visit_date          = EXCLUDED.visit_date,
                      scheduled_tech_id   = EXCLUDED.scheduled_tech_id,
                      actual_tech_id      = EXCLUDED.actual_tech_id,
                      started_at          = EXCLUDED.started_at,
                      ended_at            = EXCLUDED.ended_at,
                      status              = EXCLUDED.status,
                      visit_type          = EXCLUDED.visit_type,
                      price_cents         = COALESCE(EXCLUDED.price_cents, maintenance.visits.price_cents),
                      billing_method      = EXCLUDED.billing_method,
                      office              = EXCLUDED.office,
                      notes               = EXCLUDED.notes,
                      external_source     = EXCLUDED.external_source,
                      updated_at          = now()
                    RETURNING id
                    """, v
                )
                visit_ids.append(cur.fetchone()[0])
                stats["visits_upserted"] += 1

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM maintenance.chem_readings WHERE visit_id = ANY(%s::uuid[])",
                (visit_ids,),
            )
            cur.execute(
                "DELETE FROM maintenance.consumables_usage WHERE visit_id = ANY(%s::uuid[])",
                (visit_ids,),
            )
            cur.execute(
                "DELETE FROM maintenance.visit_tasks WHERE visit_id = ANY(%s::uuid[])",
                (visit_ids,),
            )

        chem_rows = []
        for visit_id, extras in zip(visit_ids, per_row_extras):
            pool_id = extras["pool_id"]
            chem = extras["chem"]
            if not pool_id or not chem:
                continue
            chem_rows.append({
                "visit_id": visit_id,
                "pool_id": pool_id,
                "ph": chem.get("ph"),
                "free_chlorine": chem.get("free_chlorine"),
                "total_chlorine": chem.get("total_chlorine"),
                "alkalinity": chem.get("alkalinity"),
                "cya": chem.get("cya"),
                "salt": chem.get("salt"),
                "calcium_hardness": chem.get("calcium_hardness"),
            })

        if chem_rows:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """INSERT INTO maintenance.chem_readings
                       (visit_id, pool_id, ph, free_chlorine, total_chlorine,
                        alkalinity, cya, salt, calcium_hardness)
                       VALUES (%(visit_id)s, %(pool_id)s, %(ph)s, %(free_chlorine)s,
                               %(total_chlorine)s, %(alkalinity)s, %(cya)s, %(salt)s,
                               %(calcium_hardness)s)""",
                    chem_rows,
                    page_size=200,
                )
                stats["chem_readings_inserted"] = len(chem_rows)

        cons_rows = []
        for visit_id, extras in zip(visit_ids, per_row_extras):
            pool_id = extras["pool_id"]
            for c in extras["consumables"]:
                item_name = c["item_name"]
                lookup_id = resolvers["items_by_name"].get(item_name.upper().strip())
                if lookup_id is None:
                    stats["consumables_unresolved_items"][item_name] += 1
                cons_rows.append({
                    "visit_id": visit_id,
                    "pool_id": pool_id,
                    "item_id": lookup_id,
                    "item_name": item_name,
                    "quantity": c["quantity"],
                    "source": source,
                })

        if cons_rows:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """INSERT INTO maintenance.consumables_usage
                       (visit_id, pool_id, item_id, item_name, quantity, source)
                       VALUES (%(visit_id)s, %(pool_id)s, %(item_id)s, %(item_name)s,
                               %(quantity)s, %(source)s)""",
                    cons_rows,
                    page_size=500,
                )
                stats["consumables_inserted"] = len(cons_rows)

        task_rows = []
        for visit_id, extras in zip(visit_ids, per_row_extras):
            pool_id = extras["pool_id"]
            for t in extras["visit_tasks"]:
                task_rows.append({
                    "visit_id":  visit_id,
                    "pool_id":   pool_id,
                    "task_name": t["task_name"],
                    "completed": t["completed"],
                    "source":    source,
                })

        if task_rows:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """INSERT INTO maintenance.visit_tasks
                       (visit_id, pool_id, task_name, completed, source)
                       VALUES (%(visit_id)s, %(pool_id)s, %(task_name)s,
                               %(completed)s, %(source)s)""",
                    task_rows,
                    page_size=500,
                )
                stats["visit_tasks_inserted"] = len(task_rows)

        conn.commit()

        stats["consumables_unresolved_items"] = dict(
            sorted(stats["consumables_unresolved_items"].items(), key=lambda kv: -kv[1])[:20]
        )
        return stats

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Windmill entry ───────────────────────────────────────────────────────────

def main(canonical_rows, supabase_connection, source="ion"):
    return upsert_canonical(canonical_rows, supabase_connection, source=source)
