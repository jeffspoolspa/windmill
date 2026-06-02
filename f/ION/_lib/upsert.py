# requirements:
# psycopg2-binary

"""
f/ION/_lib/upsert

Layer 3 of the ION ingest pipeline:
    parser   ->  raw ION dicts
    normalize ->  canonical-shaped dicts (this is the input)
    UPSERT   ->  maintenance.visits / chem_readings / consumables_usage / visit_tasks
                  + auto-create public.pools

v9 — visit grain = ONE ROW PER ION LOG, natural key
(service_location_id, scheduled_date, service_type, pool_id, started_at). ion_task_id
is an attribute (rate-attributed; provisional at rate-ambiguous locations, fixed by
the EventID correction pass). is_serviceable from normalize. Billable count is a
promise-level COUNT(DISTINCT date) FILTER (is_serviceable). The service_location
resolver is 4-tier (exact addr+name -> unique addr -> fuzzy name at shared addr ->
unique-name fallback) so ION report identity drift vs our QBO-synced records (name
spellings, street typos, contact-vs-account names) no longer drops visits.

Public API:
    upsert_canonical(canonical_rows, supabase_connection) -> stats dict
"""

import difflib
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
    sl_name_by_id = {}
    sl_by_name = defaultdict(set)  # n_name -> {sl_id}; for the unique-name fallback
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sl.id, sl.street, c.display_name
            FROM public.service_locations sl
            JOIN public."Customers" c ON c.id = sl.account_id
            WHERE sl.is_active
        """)
        for sl_id, street, display_name in cur.fetchall():
            n_name = normalize_customer_name(display_name or "")
            if n_name:
                sl_by_name[n_name].add(sl_id)
            n_addr = normalize_address(street or "")
            if not n_addr:
                continue
            sl_by_addr_name[(n_addr, n_name)] = sl_id
            sl_by_addr_only[n_addr].append(sl_id)
            sl_name_by_id[sl_id] = n_name

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

    # Active tasks per service_location, WITH their rate/billing so a per-pool
    # visit can attribute to the RIGHT task at a multi-contract location
    # (e.g. WINDING RIVER = a $85 POOL MAINTENANCE task + $50 CHEMICAL TESTING
    # tasks). Keyed by sl -> [task meta]; each meta carries the (max) per-visit
    # rate, flat amount, billing_method, and its (day, tech) schedule slots.
    tasks_by_sl = defaultdict(list)
    task_meta = {}  # task_id -> meta dict (also the element stored in tasks_by_sl)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.service_location_id, COALESCE(t.ion_task_id, ts.ion_task_id),
                   ts.id, ts.day_of_week, ts.tech_employee_id,
                   ts.billing_method, ts.price_per_visit_cents, ts.flat_rate_monthly_cents
            FROM maintenance.tasks t
            LEFT JOIN maintenance.task_schedules ts ON ts.task_id = t.id AND ts.active = true
            WHERE t.status IN ('active','paused')
        """)
        for task_id, sl_id, ion_task_id, sched_id, dow, tech_id, bm, rate, flat in cur.fetchall():
            m = task_meta.get(task_id)
            if m is None:
                m = {"task_id": task_id, "ion_task_id": ion_task_id, "rate": None,
                     "flat": None, "billing_method": None, "schedules": []}
                task_meta[task_id] = m
                tasks_by_sl[sl_id].append(m)
            if ion_task_id is not None and m["ion_task_id"] is None:
                m["ion_task_id"] = ion_task_id
            if rate is not None and (m["rate"] is None or rate > m["rate"]):
                m["rate"] = rate
            if flat is not None and m["flat"] is None:
                m["flat"] = flat
            if bm and not m["billing_method"]:
                m["billing_method"] = bm
            if sched_id is not None:
                m["schedules"].append((sched_id, dow, tech_id))

    # Rate-ambiguous locations: a service_location with >1 active task at the SAME
    # per-visit rate (e.g. WINDING RIVER's two $50 chem tasks + two $85 tasks). The
    # bulk report can't tell these apart (identical service type + price) -> the
    # rate resolver picks ONE arbitrarily here, and the EventID correction pass
    # (f/ION/_lib/correct_ambiguous_visits) fixes task_id/ion_task_id afterward.
    # Flagged so the upsert can record which visits are provisional.
    ambiguous_sl = set()
    for sl_id, metas in tasks_by_sl.items():
        rates_seen = defaultdict(int)
        for m in metas:
            rates_seen[(m["rate"], m["billing_method"])] += 1
        if any(n > 1 for n in rates_seen.values()):
            ambiguous_sl.add(sl_id)

    return {
        "sl_by_addr_name": sl_by_addr_name,
        "sl_by_addr_only": sl_by_addr_only,
        "sl_name_by_id": sl_name_by_id,
        "sl_by_name": {k: list(v) for k, v in sl_by_name.items()},
        "tech_by_username": tech_by_username,
        "items_by_name": items_by_name,
        "tasks_by_sl": dict(tasks_by_sl),
        "ambiguous_sl": ambiguous_sl,
    }


def _choose_task(candidates, price_cents, billing_method):
    """Pick which of a location's active tasks a visit belongs to.

    Single task -> it. Multi-contract location -> attribute by RATE so each
    visit lands on the task billed at its price (a $85 POOL MAINTENANCE visit ->
    the $85 task; a $50 CHEMICAL TESTING visit -> a $50 task). Same-rate ties are
    arbitrary but harmless: the customer-month expected (sum of rate x visits) is
    identical whichever same-rate task wins.
    """
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
    """Returns (task_id, task_schedule_id, scheduled_tech_id, ion_task_id) or all None.

    Step 1: pick the task at this location whose rate matches the visit (handles
    multi-contract communities). At rate-AMBIGUOUS locations (>1 task at the same
    rate, e.g. WINDING RIVER) this pick is provisional — the EventID correction
    pass fixes it later. Step 2: pick the (day, tech) schedule slot:
      1. matching day_of_week AND tech_employee_id == actual_tech
      2. matching day_of_week (any tech) — reassignment case
      3. None — off-schedule (make-up, QC, etc.)
    """
    candidates = resolvers["tasks_by_sl"].get(service_location_id, [])
    chosen = _choose_task(candidates, price_cents, billing_method)
    if chosen is None:
        return None, None, None, None
    task_id = chosen["task_id"]
    ion_task_id = chosen.get("ion_task_id")

    schedules = chosen["schedules"]
    if not schedules:
        return task_id, None, None, ion_task_id

    # Compute day_of_week (Postgres convention: 0=Sunday, 6=Saturday)
    if hasattr(visit_date, "weekday"):
        # Python's weekday: 0=Monday, 6=Sunday — convert
        py_dow = visit_date.weekday()
        pg_dow = (py_dow + 1) % 7
    else:
        return task_id, None, None, ion_task_id

    # Tier 1: same day AND same tech
    for sched_id, dow, tech_id in schedules:
        if dow == pg_dow and tech_id == actual_tech_id:
            return task_id, sched_id, tech_id, ion_task_id
    # Tier 2: same day, any tech (reassignment case)
    for sched_id, dow, tech_id in schedules:
        if dow == pg_dow:
            return task_id, sched_id, tech_id, ion_task_id
    return task_id, None, None, ion_task_id


def resolve_service_location_id(resolvers, addr, name):
    n_addr = normalize_address(addr or "")
    if not n_addr:
        return None
    n_name = normalize_customer_name(name or "")
    if n_name and (n_addr, n_name) in resolvers["sl_by_addr_name"]:
        return resolvers["sl_by_addr_name"][(n_addr, n_name)]
    candidates = resolvers["sl_by_addr_only"].get(n_addr, [])
    if len(candidates) == 1:
        return candidates[0]
    # NAME-DRIFT TOLERANCE (Carter's choice, 2026-06-02): when an ION report address
    # maps to MULTIPLE service_locations (shared address / family / co-located), the
    # exact (addr, name) match can fail because ION's report spells the customer name
    # slightly differently than our QBO-synced display_name (LEICHART vs LEICHERT,
    # LESLIER vs LESLIE, STACIE vs STACY, the "- 210"/"- SSI" suffixes, ...). We can't
    # durably fix display_name (QBO sync reverts it), so we pick the candidate whose
    # name is the CLEAR closest match. Conservative: require a high absolute score AND
    # a clear margin over the runner-up so we never silently mis-attribute a genuinely
    # different co-located customer. Skip "deleted"-named dup rows.
    if len(candidates) > 1 and n_name:
        names = resolvers.get("sl_name_by_id", {})
        scored = []
        for sl_id in candidates:
            cn = names.get(sl_id, "")
            if not cn or "deleted" in cn.lower():
                continue
            scored.append((difflib.SequenceMatcher(None, n_name, cn).ratio(), sl_id))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15):
            return scored[0][1]

    # TIER 4 — unique-name fallback: the address couldn't place this visit (typo on
    # ION's side e.g. Taylor "534" vs real "4534", or on QBO's side e.g. PARRISH
    # "CICLE", or a 2nd property with no sl e.g. Johnson). Neither is durably fixable
    # in our mirror (QBO sync overwrites both street + display_name). If the report's
    # customer name (>=2 tokens) matches EXACTLY ONE active service_location globally,
    # attribute the visit to it. Conservative: exact normalized-name + global
    # uniqueness only -> high confidence it's that customer.
    if n_name and len(n_name.split()) >= 2:
        by_name = resolvers.get("sl_by_name", {}).get(n_name, [])
        if len(by_name) == 1:
            return by_name[0]
    return None  # ambiguous or unknown


def resolve_tech_id(resolvers, username):
    if not username:
        return None
    return resolvers["tech_by_username"].get(normalize_tech_name(username))


# ─── value derivation ─────────────────────────────────────────────────────────

def combine_datetime(d, time_str):
    """date + 'HH:MM AM/PM' string -> isoformat str, treated as US/Eastern."""
    if not d or not time_str:
        return None
    if isinstance(d, str):
        try:
            d = _date.fromisoformat(d)
        except ValueError:
            return None
    try:
        t = datetime.strptime(time_str.strip(), "%I:%M %p").time()
        # No tz attached — Postgres column is timestamptz with default tz
        # AT TIME ZONE 'America/New_York' could be applied in SQL later if needed.
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
    """Take canonical-shaped rows from f/ION/_lib/normalize and write to maintenance.*

    Idempotency: visits UPSERT on the per-log natural key
    (service_location_id, scheduled_date, service_type, pool_id, started_at).
    chem_readings + consumables_usage + visit_tasks are DELETE-then-INSERT for the
    touched visit_ids.
    """
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

        # Build prepared visit rows + indexable links to readings/consumables
        visit_buffer = []   # list of dicts ready to insert
        per_row_extras = []  # parallel to visit_buffer: {chem, consumables}

        for row in canonical_rows:
            v = row.get("visits", {}) or {}
            chem = row.get("chem_readings", {}) or {}
            pool_meta = row.get("pools", {}) or {}
            consumables = row.get("consumables_usage_rows", []) or []
            visit_tasks = row.get("visit_tasks_rows", []) or []

            # ION's "Address1" is the customer name as a ship-to label;
            # "Address2" is the actual street. Try Address2 first; fall back
            # to Address1 if Address2 is empty.
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
                # SJC-style: ION's "Customer" column can be a CONTACT name (e.g.
                # "Winters, Karen") while "Address1" carries the account/ship-to label
                # that matches our display_name ("SJC PROPERTIES"). Try Address1 as the
                # name before giving up.
                a1 = v.get("_address1")
                if a1 and a1 != v.get("_customer_name"):
                    sl_id = resolve_service_location_id(resolvers, primary_addr, a1)
                    if sl_id is None and secondary_addr:
                        sl_id = resolve_service_location_id(resolvers, secondary_addr, a1)
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
                continue  # can't ingest without a date

            started_at = combine_datetime(visit_date, v.get("_start_time_str"))
            ended_at = combine_datetime(visit_date, v.get("_end_time_str"))
            price_cents = v.get("price_cents") or 0
            visit_type = derive_visit_type(v.get("_service_type"), price_cents)
            billing_method = derive_billing_method(v.get("_invoice_type"))

            # Pool resolution (auto-create if needed)
            pool_name = pool_meta.get("_pool_name")
            pool_id, was_created = get_or_create_pool(conn, sl_id, pool_name, source=source)
            if was_created:
                stats["pools_created"] += 1

            # Task + schedule resolution. Determines:
            #   task_id          — which recurring contract this visit belongs to
            #   task_schedule_id — which (day, tech) slot it fills
            #   scheduled_tech_id — who SHOULD have done it per the schedule
            #   ion_task_id      — the matched task's ION task id (provisional at
            #                       rate-ambiguous locations; fixed by EventID pass)
            task_id, task_schedule_id, scheduled_tech, ion_task_id = resolve_task_and_schedule(
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

            # Serviceability (normalize set this from Start==End / Actual==0); default
            # True if the mapping didn't produce a visits block for this row.
            is_serviceable = bool(v.get("is_serviceable", True))
            if not is_serviceable:
                stats.setdefault("non_serviceable", 0)
                stats["non_serviceable"] += 1
            # Provisional attribution at rate-ambiguous locations -> count for the
            # EventID correction pass to target.
            if sl_id in resolvers.get("ambiguous_sl", set()):
                stats.setdefault("ambiguous_rows", 0)
                stats["ambiguous_rows"] += 1

            visit_buffer.append({
                "service_location_id": sl_id,
                "pool_id": pool_id,
                "service_type": v.get("_service_type"),
                "task_id": task_id,
                "ion_task_id": ion_task_id,
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
                "is_serviceable": is_serviceable,
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

        # UPSERT visits, capturing returned ids
        visit_ids = []
        with conn.cursor() as cur:
            for v in visit_buffer:
                cur.execute(
                    """
                    INSERT INTO maintenance.visits
                      (service_location_id, pool_id, service_type, task_id, ion_task_id, task_schedule_id,
                       scheduled_date, visit_date,
                       scheduled_tech_id, actual_tech_id, started_at, ended_at,
                       status, visit_type, price_cents, billing_method, is_serviceable,
                       office, notes, external_source)
                    VALUES
                      (%(service_location_id)s, %(pool_id)s, %(service_type)s, %(task_id)s, %(ion_task_id)s, %(task_schedule_id)s,
                       %(scheduled_date)s, %(visit_date)s,
                       %(scheduled_tech_id)s, %(actual_tech_id)s, %(started_at)s, %(ended_at)s,
                       %(status)s, %(visit_type)s, %(price_cents)s, %(billing_method)s, %(is_serviceable)s,
                       %(office)s, %(notes)s, %(external_source)s)
                    ON CONFLICT (service_location_id, scheduled_date, service_type, pool_id, started_at) DO UPDATE SET
                      task_id             = COALESCE(EXCLUDED.task_id, maintenance.visits.task_id),
                      ion_task_id         = COALESCE(EXCLUDED.ion_task_id, maintenance.visits.ion_task_id),
                      task_schedule_id    = COALESCE(EXCLUDED.task_schedule_id, maintenance.visits.task_schedule_id),
                      visit_date          = EXCLUDED.visit_date,
                      scheduled_tech_id   = EXCLUDED.scheduled_tech_id,
                      actual_tech_id      = EXCLUDED.actual_tech_id,
                      ended_at            = EXCLUDED.ended_at,
                      status              = EXCLUDED.status,
                      visit_type          = EXCLUDED.visit_type,
                      price_cents         = COALESCE(EXCLUDED.price_cents, maintenance.visits.price_cents),
                      billing_method      = EXCLUDED.billing_method,
                      is_serviceable      = EXCLUDED.is_serviceable,
                      office              = EXCLUDED.office,
                      notes               = EXCLUDED.notes,
                      external_source     = EXCLUDED.external_source,
                      updated_at          = now()
                    RETURNING id
                    """, v
                )
                visit_ids.append(cur.fetchone()[0])
                stats["visits_upserted"] += 1

        # DELETE existing chem + consumables + visit_tasks for these visits,
        # then INSERT. Same DELETE-then-INSERT idempotency pattern across all
        # three — re-running an ingestion for the same visit_ids replaces
        # the per-visit detail rather than appending duplicates.
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

        # Insert chem_readings (one per row, keyed by visit_id + pool_id)
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

        # Insert consumables_usage (one row per item per visit)
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

        # Insert visit_tasks (one row per checked-or-unchecked task per visit).
        # Canonical task_name comes from f/ION/_lib/normalize.resolve_task_alias,
        # which maps raw ION column headers (Brsh, Vac, Cell, ...) to
        # snake_case canonical names.
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

        # Make defaultdict JSON-friendly
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
    """Smoke entry. Pass `canonical_rows` from f/ION/_lib/normalize's output."""
    return upsert_canonical(canonical_rows, supabase_connection, source=source)
