# requirements:
# psycopg2-binary

"""
f/ION/_lib/normalize

Normalizes parser-output rows (raw ION dicts) into canonical-shaped dicts
ready for upsert into maintenance.* tables. Mappings live in
public.app_config (key='ion_field_mappings') so they can be edited via UI
without code changes.

Architecture (3 layers):
    parser.py     ->  raw ION dicts        ({Customer, FC, Date, ...})
    normalize.py  ->  canonical dicts      ({visits: {...}, chem_readings: {...}})
    upsert.py     ->  Supabase rows        (resolves FKs, COPY into tables)

This module is layer 2. It does NOT do DB writes for the canonical data —
that's the upsert step's job. It DOES write unmapped-field tracking back
into app_config so the UI can show "fields ION sent that we don't know
what to do with."

Public API:
    normalize_rows(parser_output, supabase_connection)
        -> {canonical_rows, unmapped_summary, transform_errors}
    update_unmapped_in_config(supabase_connection, unmapped_summary)
        -> None  (merges into app_config row)
    main(parser_output, supabase_connection, write_unmapped=True)
        -> Windmill entry point (smoke-testable)
"""

import json
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2


# ─── Transform registry ────────────────────────────────────────────────────────
# Each transform takes a raw string value from the parser and returns a
# canonical-typed value. Names match the "transform" field in app_config.

def _identity(v):
    return v


def _parse_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _parse_money_to_cents(v):
    """'$45.00' -> 4500;  '1,234.56' -> 123456;  empty -> None."""
    if v is None or v == "":
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    try:
        return int(round(float(s) * 100))
    except (ValueError, TypeError):
        return None


def _parse_date_mdy(v):
    """'04/16/2026' -> date(2026, 4, 16).  Empty/None -> None."""
    if v is None or v == "":
        return None
    try:
        return datetime.strptime(str(v).strip(), "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def _parse_date_iso(v):
    if v is None or v == "":
        return None
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _yes_no_to_bool(v):
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in ("yes", "true", "1", "y"):
        return True
    if s in ("no", "false", "0", "n"):
        return False
    return None


# ─── Non-serviceable detection (the "+1" fix) ───────────────────────────────────
# ION logs some service events that it does NOT bill: holidays, no-access, skips.
# Carter confirmed these are NON-SERVICEABLE visits. The bulk CompletedLogDetail
# report has NO explicit serviceable/status column, but non-serviceable rows are
# identifiable by ZERO DURATION:
#   - Start == End  (e.g. "10:56 AM" == "10:56 AM" — the tech clocked no time)
#   - Actual == 0   (actual minutes on site is zero)
# Note: Price can still be non-zero ($50) on these rows, so a price>0 filter MISSES
# them — that is exactly the over-count that produced the WINDING RIVER "+1"
# (05-25 Memorial Day "Holiday" CHEMICAL TESTING logs). ION/QBO correctly do not
# charge them; our code was counting them as billable. PROOF: excluding the
# zero-duration 05-25 logs makes WINDING RIVER tasks 5333857 (21->20) and
# 5333849 (8->7) reconcile EXACTLY to the $3,305 invoice.
#
# Conservative rule: a row is non-serviceable ONLY when we have positive evidence
# of zero duration. Missing/blank times are NOT treated as non-serviceable (absence
# of data != evidence of a skip) — those stay serviceable so we never silently drop
# a real visit we simply lack a clock time for.

def _is_serviceable(flat: dict) -> bool:
    """Return False for zero-duration (non-serviceable) ION log rows, True otherwise.

    Reads the raw ION report fields (pre-mapping): 'Start', 'End', 'Actual'.
    """
    start = (str(flat.get("Start") or "")).strip()
    end = (str(flat.get("End") or "")).strip()
    actual = _parse_float(flat.get("Actual"))

    # Evidence 1: clock shows no time on site (start time == end time, both present).
    if start and end and start == end:
        return False
    # Evidence 2: actual minutes explicitly zero (independent corroboration; some
    # rows carry Actual without usable Start/End).
    if actual is not None and actual == 0:
        return False
    return True


TRANSFORMS = {
    "identity":             _identity,
    "parse_float":          _parse_float,
    "parse_int":            _parse_int,
    "parse_money_to_cents": _parse_money_to_cents,
    "parse_date_mdy":       _parse_date_mdy,
    "parse_date_iso":       _parse_date_iso,
    "yes_no_to_bool":       _yes_no_to_bool,
}


# ─── Task alias + definition catalog (encoded in code, like consumables) ──────
# Source of truth for the per-visit task checklist. ION reports use short
# column headers ("Brsh", "Vac", "Cell") which need normalization to
# canonical names before insertion into maintenance.visit_tasks.
#
# Why these live in code instead of a DB-driven table:
#   - The set is small (~25 entries) and changes rarely.
#   - Keeping it in code means the upsert step has zero extra DB lookups
#     per row — same pattern that consumables follow (item_name resolution
#     happens in upsert.py via the items table, but the structural mapping
#     stays in code).
#   - Adding/changing a task is a code change (PR-reviewed) rather than a
#     row insert someone makes in the dashboard with no record.
#
# Adding a new task:
#   1. Add the row to TASK_DEFINITIONS with display_name + category +
#      display_order.
#   2. Add every raw ION header that should resolve to it in TASK_ALIASES.
#   3. No migration needed — task_name is a free-text column in
#      maintenance.visit_tasks, so new canonical names just start appearing.

TASK_ALIASES: dict[str, str] = {
    # equipment
    "Filt":  "cleaned_filter",
    "Cell":  "cleaned_salt_cell",
    "Service Pump": "service_pump",
    "HEAT":  "heater_working",
    # cleaning
    "Vac":   "vacuum_pool",
    "Brsh":  "brushed_pool",
    "PBsk":  "emptied_pump_baskets",
    "SBsk":  "emptied_skimmer_baskets",
    "Bag":   "emptied_cleaner_bag",
    "Net":   "skim_net_surface",
    # water management
    "Fill":  "drained_filled",
    "Added Water": "added_water",
    # inspection
    "Safety Inspection": "safety_inspection",
    # other
    "Customer Tabs (Not to be billed)": "customer_tabs",
    "WELL":  "install_well_points",
    "DECK":  "deck_repair",
    "TILE":  "redo_tile",
    "REGR":  "regrout",
    "PP":    "painted_pool",
    "FT":    "fibertech_coating",
    "BT":    "bead_track_replace",
    "BL":    "bead_lock_replace",
    "FR":    "floor_repair",
}

# canonical_name -> metadata for UI rendering / grouping / sort order
TASK_DEFINITIONS: dict[str, dict] = {
    "cleaned_filter":          {"display_name": "Cleaned Filter",          "category": "equipment",  "display_order": 1},
    "cleaned_salt_cell":       {"display_name": "Cleaned Salt Cell",       "category": "equipment",  "display_order": 2},
    "service_pump":            {"display_name": "Service Pump",            "category": "equipment",  "display_order": 3},
    "heater_working":          {"display_name": "Heater Working",          "category": "equipment",  "display_order": 4},
    "vacuum_pool":             {"display_name": "Vacuum Pool",             "category": "cleaning",   "display_order": 5},
    "brushed_pool":            {"display_name": "Brushed Pool",            "category": "cleaning",   "display_order": 6},
    "emptied_pump_baskets":    {"display_name": "Emptied Pump Baskets",    "category": "cleaning",   "display_order": 7},
    "emptied_skimmer_baskets": {"display_name": "Emptied Skimmer Baskets", "category": "cleaning",   "display_order": 8},
    "emptied_cleaner_bag":     {"display_name": "Emptied Cleaner Bag",     "category": "cleaning",   "display_order": 9},
    "skim_net_surface":        {"display_name": "Skim/Net Surface",        "category": "cleaning",   "display_order": 10},
    "drained_filled":          {"display_name": "Drained/Filled",          "category": "water_mgmt", "display_order": 11},
    "added_water":             {"display_name": "Added Water",             "category": "water_mgmt", "display_order": 12},
    "safety_inspection":       {"display_name": "Safety Inspection",       "category": "inspection", "display_order": 13},
    "customer_tabs":           {"display_name": "Customer Tabs (Not to be billed)", "category": "other", "display_order": 14},
    "install_well_points":     {"display_name": "Install Well Points",     "category": "other",      "display_order": 15},
    "deck_repair":             {"display_name": "Deck Repair",             "category": "other",      "display_order": 16},
    "redo_tile":               {"display_name": "Redo Tile?",              "category": "other",      "display_order": 17},
    "regrout":                 {"display_name": "Regrout?",                "category": "other",      "display_order": 18},
    "painted_pool":            {"display_name": "Painted Pool?",           "category": "other",      "display_order": 19},
    "fibertech_coating":       {"display_name": "Fibertech Coating?",      "category": "other",      "display_order": 20},
    "bead_track_replace":      {"display_name": "Bead Track Replace",      "category": "other",      "display_order": 21},
    "bead_lock_replace":       {"display_name": "Bead Lock Replace",       "category": "other",      "display_order": 22},
    "floor_repair":            {"display_name": "Floor Repair",            "category": "other",      "display_order": 23},
}


def resolve_task_alias(raw_name: str) -> str:
    """Map an ION raw column header (e.g. 'Brsh') to its canonical task name
    (e.g. 'brushed_pool'). Falls back to a slugified version of the raw name
    if no alias is registered — that way we capture the data anyway and a
    later code change can add the alias without losing rows.
    """
    if raw_name in TASK_ALIASES:
        return TASK_ALIASES[raw_name]
    # Slug fallback: 'Some New Task!' -> 'some_new_task'
    import re as _re
    slug = _re.sub(r'[^a-z0-9]+', '_', raw_name.lower()).strip('_')
    return slug or "unknown"


# ─── DB helpers ────────────────────────────────────────────────────────────────

def _connect(supabase_connection: dict):
    return psycopg2.connect(
        host=supabase_connection["host"],
        port=supabase_connection["port"],
        dbname=supabase_connection["dbname"],
        user=supabase_connection["user"],
        password=supabase_connection["password"],
        connect_timeout=10,
    )


def load_mappings(supabase_connection: dict, key: str = "ion_field_mappings") -> dict:
    """Load the mapping config blob from public.app_config."""
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.app_config WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"app_config key '{key}' not found")
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    finally:
        conn.close()


# ─── Core normalize fn ─────────────────────────────────────────────────────────

def _flatten_parser_row(raw_row: dict) -> tuple[dict, dict, dict]:
    """The parser emits flat ION fields PLUS nested _readings/_tasks/_consumables.
    Flatten readings into the same lookup dict (their keys are unique ION
    names like 'FC' that are mapped via app_config to chem_readings columns).
    Keep tasks and consumables separate — they unpivot to their own tables
    via local alias mapping (no app_config lookup).

    Returns: (flat, tasks_raw, consumables_raw)
    """
    flat: dict = {}
    tasks: dict = {}
    consumables: dict = {}
    for k, v in raw_row.items():
        if k == "_readings":
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    flat[sub_k] = sub_v
        elif k == "_tasks":
            tasks = v if isinstance(v, dict) else {}
        elif k == "_consumables":
            consumables = v if isinstance(v, dict) else {}
        else:
            flat[k] = v
    return flat, tasks, consumables


def normalize_row(
    raw_row: dict,
    mapping_index: dict,
    skip_set: set,
) -> tuple[dict, dict, list]:
    """Apply mappings to one parser-output row.

    Returns:
        canonical:     {table_name -> {field_name -> typed_value}}
                       Plus optional 'consumables_usage_rows' as a list of
                       {item_name, quantity} dicts (downstream upsert
                       resolves item_name -> public.items.id).
        unmapped:      {source_field -> sample_value_str}
        bad_transform: [(source_field, transform_name), ...]
    """
    flat, tasks_raw, consumables = _flatten_parser_row(raw_row)

    canonical: dict[str, dict] = defaultdict(dict)
    unmapped: dict[str, str] = {}
    bad_transform: list = []

    for source_field, raw_value in flat.items():
        if source_field in skip_set:
            continue
        m = mapping_index.get(source_field)
        if m is None:
            if raw_value is not None and raw_value != "":
                unmapped[source_field] = str(raw_value)[:120]
            continue
        fn = TRANSFORMS.get(m.get("transform", "identity"))
        if fn is None:
            bad_transform.append((source_field, m.get("transform")))
            continue
        try:
            transformed = fn(raw_value)
        except Exception:  # noqa: BLE001 — we want to never break the pipeline on a bad value
            bad_transform.append((source_field, m.get("transform")))
            continue
        canonical[m["canonical_table"]][m["canonical_field"]] = transformed

    # Non-serviceable flag (zero-duration logs ION doesn't bill). Computed from the
    # raw report fields, attached to the visit so the upsert can persist it and the
    # promise builder can exclude it from billable_visit_count. Defaults to True
    # (serviceable) — we only flip to False on positive zero-duration evidence.
    if "visits" in canonical or any(m["canonical_table"] == "visits" for m in mapping_index.values()):
        canonical["visits"]["is_serviceable"] = _is_serviceable(flat)

    # Tasks: apply local alias map (TASK_ALIASES), convert Yes/No → boolean,
    # emit as visit_tasks_rows for the upsert step. Pattern mirrors how
    # consumables are handled below — structural mapping in code, row
    # insertion in upsert.py.
    if tasks_raw:
        task_rows: list = []
        for raw_name, raw_value in tasks_raw.items():
            completed = _yes_no_to_bool(raw_value)
            if completed is None:
                # Empty cell — skip (tech didn't fill it in). Different from
                # "No" which means tech explicitly marked it not done.
                continue
            canonical_name = resolve_task_alias(raw_name)
            task_rows.append({
                "task_name":    canonical_name,
                "raw_task_name": raw_name,
                "completed":    completed,
            })
        if task_rows:
            canonical["visit_tasks_rows"] = task_rows

    if consumables:
        items: list = []
        for item_name, qty in consumables.items():
            qty_f = _parse_float(qty)
            if qty_f in (None, 0):
                continue
            items.append({"item_name": item_name, "quantity": qty_f})
        if items:
            canonical["consumables_usage_rows"] = items

    return dict(canonical), unmapped, bad_transform


def normalize_rows(parser_output: dict, supabase_connection: dict) -> dict:
    """Process every row from a parser output.

    Args:
        parser_output: dict emitted by parser.py — has 'rows' key.
        supabase_connection: dict with host/port/dbname/user/password.

    Returns:
        {
          'canonical_rows': [...],        # one entry per source row
          'unmapped_summary': [           # de-duped, sorted by occurrence
            {'source_field': '...', 'occurrence_count': N, 'sample_values': [...]}
          ],
          'transform_errors': [...],
          'config_version': N,
          'config_updated_at': ISO,
        }
    """
    config = load_mappings(supabase_connection)
    mappings = config.get("mappings", [])
    skip_fields = config.get("skip_fields", [])

    mapping_index = {m["source_field"]: m for m in mappings}
    skip_set = {s["source_field"] for s in skip_fields}

    canonical_rows: list = []
    unmapped_seen: dict[str, dict] = defaultdict(lambda: {"count": 0, "samples": []})
    transform_errors: list = []

    for raw_row in parser_output.get("rows", []):
        canonical, unmapped, bad = normalize_row(raw_row, mapping_index, skip_set)
        canonical_rows.append(canonical)
        for sf, sample in unmapped.items():
            slot = unmapped_seen[sf]
            slot["count"] += 1
            if len(slot["samples"]) < 5 and sample not in slot["samples"]:
                slot["samples"].append(sample)
        transform_errors.extend(bad)

    unmapped_summary = [
        {"source_field": sf, "occurrence_count": d["count"], "sample_values": d["samples"]}
        for sf, d in unmapped_seen.items()
    ]
    unmapped_summary.sort(key=lambda x: -x["occurrence_count"])

    return {
        "canonical_rows": canonical_rows,
        "unmapped_summary": unmapped_summary,
        "transform_errors": transform_errors,
        "config_version": config.get("version"),
    }


# ─── Write unmapped back to app_config ─────────────────────────────────────────

def update_unmapped_in_config(
    supabase_connection: dict,
    unmapped_summary: list,
    key: str = "ion_field_mappings",
    updated_by: str = "ion_normalize",
) -> dict:
    """Merge new unmapped fields into app_config.value['unmapped_fields'].

    For each field already in the list: bump occurrence_count, refresh
    last_seen_at, union sample_values (capped at 10).

    For new fields: append with first_seen_at and last_seen_at = now.
    """
    conn = _connect(supabase_connection)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM public.app_config WHERE key = %s FOR UPDATE",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"app_config key '{key}' not found")
            config = row[0] if isinstance(row[0], dict) else json.loads(row[0])

            existing = {
                u["source_field"]: u
                for u in config.get("unmapped_fields", [])
            }
            now_iso = datetime.now(timezone.utc).isoformat()

            for u in unmapped_summary:
                sf = u["source_field"]
                if sf in existing:
                    existing[sf]["occurrence_count"] = (
                        existing[sf].get("occurrence_count", 0) + u["occurrence_count"]
                    )
                    existing[sf]["last_seen_at"] = now_iso
                    seen = set(existing[sf].get("sample_values") or [])
                    for s in u["sample_values"]:
                        if s in seen:
                            continue
                        existing[sf].setdefault("sample_values", []).append(s)
                        seen.add(s)
                        if len(existing[sf]["sample_values"]) >= 10:
                            break
                else:
                    existing[sf] = {
                        **u,
                        "first_seen_at": now_iso,
                        "last_seen_at": now_iso,
                    }

            config["unmapped_fields"] = list(existing.values())
            cur.execute(
                "UPDATE public.app_config "
                "SET value = %s, updated_at = now(), updated_by = %s "
                "WHERE key = %s",
                (json.dumps(config), updated_by, key),
            )
            conn.commit()
            return {
                "merged_count": len(unmapped_summary),
                "total_unmapped_in_config": len(config["unmapped_fields"]),
            }
    finally:
        conn.close()


# ─── Windmill entry point ──────────────────────────────────────────────────────

def main(
    parser_output: dict,
    supabase_connection: dict,
    write_unmapped: bool = True,
):
    """Smoke-testable Windmill entry. Takes parser output JSON, applies
    mappings, optionally writes unmapped fields back to app_config.
    """
    result = normalize_rows(parser_output, supabase_connection)
    update_info = None
    if write_unmapped and result["unmapped_summary"]:
        update_info = update_unmapped_in_config(
            supabase_connection,
            result["unmapped_summary"],
        )

    # Convert dates to ISO strings for JSON serialization in the response
    def _safe(v):
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

    preview = None
    if result["canonical_rows"]:
        first = result["canonical_rows"][0]
        preview = {table: {f: _safe(v) for f, v in fields.items()}
                   if isinstance(fields, dict) else fields
                   for table, fields in first.items()}

    return {
        "canonical_row_count": len(result["canonical_rows"]),
        "unmapped_distinct_count": len(result["unmapped_summary"]),
        "unmapped_summary": result["unmapped_summary"][:20],
        "transform_errors": result["transform_errors"][:10],
        "config_version": result["config_version"],
        "first_canonical_row_preview": preview,
        "unmapped_write_result": update_info,
    }
