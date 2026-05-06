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


TRANSFORMS = {
    "identity":             _identity,
    "parse_float":          _parse_float,
    "parse_int":            _parse_int,
    "parse_money_to_cents": _parse_money_to_cents,
    "parse_date_mdy":       _parse_date_mdy,
    "parse_date_iso":       _parse_date_iso,
    "yes_no_to_bool":       _yes_no_to_bool,
}


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

def _flatten_parser_row(raw_row: dict) -> tuple[dict, dict]:
    """The parser emits flat ION fields PLUS nested _readings/_tasks/_consumables.
    Flatten readings + tasks into the same lookup dict (their keys are unique
    ION names like 'FC', 'Vac', so no collision with core fields). Keep
    consumables separate — they unpivot to their own table.
    """
    flat: dict = {}
    consumables: dict = {}
    for k, v in raw_row.items():
        if k in ("_readings", "_tasks"):
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    flat[sub_k] = sub_v
        elif k == "_consumables":
            consumables = v if isinstance(v, dict) else {}
        else:
            flat[k] = v
    return flat, consumables


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
    flat, consumables = _flatten_parser_row(raw_row)

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
