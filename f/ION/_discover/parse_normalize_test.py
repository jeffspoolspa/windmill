# requirements:
# requests
# beautifulsoup4
# psycopg2-binary

"""
f/ION/_discover/parse_normalize_test

End-to-end smoke test of the FULL pipeline: fetch -> parse -> normalize -> upsert.
Skips login by re-using a captured IonSession (cookies + cfClientId + ionOrigin).

Pipeline:
    1. Picker-prime + bare-data fetch via plain HTTP (no Chromium)
    2. Parse the HTML with f/ION/_lib/parser
    3. Normalize with f/ION/_lib/normalize (reads mappings from app_config)
    4. Upsert into maintenance.visits / chem_readings / consumables_usage
       via f/ION/_lib/upsert (resolves FKs against Customers + employees + items;
       auto-creates pools)
"""

import os
from datetime import date

import requests

import f.ION._lib.parser as ion_parser
import f.ION._lib.normalize as ion_normalize
import f.ION._lib.upsert as ion_upsert


def _cookie_header(cookies, ion_origin):
    host = ion_origin.replace("https://", "").replace("http://", "").split("/")[0]
    parts = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if host == domain or host.endswith("." + domain):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def main(ion_session, supabase_connection, lookback_days=30, write_unmapped=True,
         start_date=None, end_date=None, probe_only=False):
    """Fetch + parse + normalize + upsert ION completed service logs.

    Date window: [start, end]. By default start = today - lookback_days and end =
    open (today). For BACKFILL, pass explicit start_date / end_date (YYYY-MM-DD)
    to fetch a bounded historical window (chunk the backfill into windows; ION's
    report + this parser + the row-by-row upsert don't want ~1.5y at once).
    probe_only=True -> fetch + parse + report counts, but SKIP normalize-write and
    upsert (read-only feasibility check; writes nothing to the DB)."""
    ion_origin = ion_session["ionOrigin"]
    cf_client_id = ion_session.get("cfClientId") or ""
    cookie_header = _cookie_header(ion_session["cookies"], ion_origin)
    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html, */*",
    }

    # STEP 1 — picker prime (bounded window if start/end given)
    start_str = start_date or date.fromordinal(date.today().toordinal() - lookback_days).strftime("%Y-%m-%d")
    end_str = end_date or ""
    print(f"STEP 1: picker prime (Start={start_str}, end={end_str or 'open'})")
    picker_url = f"{ion_origin}/reports/serviceLogs.cfm"
    picker_params = {
        "office": "", "tech": "", "Start": start_str, "end": end_str, "set": "1",
        "_cf_containerId": "rptDetail",
        "_cf_nodebug": "true", "_cf_nocache": "true",
        "_cf_clientid": cf_client_id, "_cf_rc": "1",
    }
    r = requests.get(picker_url, params=picker_params, headers=headers, allow_redirects=False, timeout=60)
    if r.status_code != 200:
        return {"ok": False, "stage": "picker", "status": r.status_code, "body_preview": r.text[:500]}
    print(f"  picker {r.status_code}, {len(r.content)} bytes")

    # STEP 2 — bare data URL
    data_url = f"{ion_origin}/reports/_xls/CompletedLogDetail.cfm"
    print(f"STEP 2: data fetch")
    r2 = requests.get(data_url, headers=headers, allow_redirects=False, timeout=180)
    if r2.status_code != 200:
        return {"ok": False, "stage": "data", "status": r2.status_code, "body_preview": r2.text[:500]}
    print(f"  data {r2.status_code}, {len(r2.content)} bytes")

    # Save raw HTML to ./shared/ for the next step (and debugging)
    html_path = "./shared/completed_log_detail.html"
    os.makedirs("./shared", exist_ok=True)
    with open(html_path, "w") as f:
        f.write(r2.text)

    # STEP 3 — parser
    print("STEP 3: parser")
    parsed = ion_parser.parse(html_path, "service_log")
    parser_meta = parsed["extraction_metadata"]
    print(f"  parser: {parser_meta['row_count']} rows, {len(parser_meta['profiles_found'])} profiles")

    # PROBE — read-only feasibility check: report what the window returned, write nothing
    if probe_only:
        return {
            "ok": True, "probe_only": True,
            "fetch": {"start_date": start_str, "end_date": end_str or None, "data_bytes": len(r2.content)},
            "parser": {
                "row_count": parser_meta["row_count"],
                "profiles_found": parser_meta["profiles_found"],
                "profile_row_counts": parser_meta["profile_row_counts"],
            },
        }

    # STEP 4 — normalize (we want the FULL canonical_rows, not just summary)
    print("STEP 4: normalize")
    norm = ion_normalize.normalize_rows(parsed, supabase_connection)
    if write_unmapped and norm["unmapped_summary"]:
        ion_normalize.update_unmapped_in_config(supabase_connection, norm["unmapped_summary"])

    # STEP 5 — upsert canonical rows into maintenance.* tables
    print(f"STEP 5: upsert ({len(norm['canonical_rows'])} canonical rows)")
    upsert_stats = ion_upsert.upsert_canonical(
        norm["canonical_rows"],
        supabase_connection,
        source="ion",
    )

    return {
        "ok": True,
        "fetch": {
            "start_date": start_str,
            "end_date": end_str or None,
            "lookback_days": lookback_days,
            "data_bytes": len(r2.content),
        },
        "parser": {
            "row_count": parser_meta["row_count"],
            "profiles_found": parser_meta["profiles_found"],
            "profile_row_counts": parser_meta["profile_row_counts"],
        },
        "normalize": {
            "canonical_row_count": len(norm["canonical_rows"]),
            "unmapped_distinct": len(norm["unmapped_summary"]),
            "transform_errors": norm["transform_errors"][:10],
            "top_unmapped": norm["unmapped_summary"][:10],
        },
        "upsert": upsert_stats,
    }
