# requirements:
# requests
# beautifulsoup4
# psycopg2-binary

"""
f/ION/_discover/parse_normalize_test

End-to-end smoke test of the parse + normalize layers, skipping login by
re-using a captured IonSession (cookies + cfClientId + ionOrigin).

Pipeline:
    1. Picker-prime + bare-data fetch via plain HTTP (no Chromium — cookies only)
    2. Parse the HTML with f/ION/_lib/parser
    3. Normalize the parsed rows with f/ION/_lib/normalize
    4. Return summary: row counts, mapping coverage, unmapped fields

This proves the entire data pipeline works end-to-end with a working session.
"""

import json
from datetime import date, datetime

import requests

# Imports from sibling Windmill scripts
import f.ION._lib.parser as ion_parser
import f.ION._lib.normalize as ion_normalize


def _cookie_header(cookies: list, ion_origin: str) -> str:
    """Build the Cookie: request header from session cookies, scoped to ION's domain."""
    host = ion_origin.replace("https://", "").replace("http://", "").split("/")[0]
    parts = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if host == domain or host.endswith("." + domain):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def main(
    ion_session: dict,
    supabase_connection: dict,
    lookback_days: int = 30,
):
    """Run the full parse+normalize pipeline for CompletedLogDetail.

    Args:
        ion_session: dict with shape {cookies, cfClientId, ionOrigin}
                     — captured via f/ION/_discover/emit_session
        supabase_connection: dict with host/port/dbname/user/password
        lookback_days: how many days back to pull (default 30)
    """
    ion_origin = ion_session["ionOrigin"]
    cf_client_id = ion_session.get("cfClientId") or ""
    cookie_header = _cookie_header(ion_session["cookies"], ion_origin)

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html, */*",
    }

    # STEP 1: prime the picker with our filters
    start_iso = (date.today().toordinal() - lookback_days)
    start_str = date.fromordinal(start_iso).strftime("%Y-%m-%d")
    picker_url = f"{ion_origin}/reports/serviceLogs.cfm"
    picker_params = {
        "office": "", "tech": "", "Start": start_str, "end": "", "set": "1",
        "_cf_containerId": "rptDetail",
        "_cf_nodebug": "true", "_cf_nocache": "true",
        "_cf_clientid": cf_client_id, "_cf_rc": "1",
    }
    print(f"STEP 1: priming picker (Start={start_str})")
    r = requests.get(picker_url, params=picker_params, headers=headers, allow_redirects=False, timeout=60)
    print(f"  picker {r.status_code}, {len(r.content)} bytes")
    if r.status_code != 200:
        return {"ok": False, "stage": "picker", "status": r.status_code, "body_preview": r.text[:500]}

    # STEP 2: fetch bare data URL
    data_url = f"{ion_origin}/reports/_xls/CompletedLogDetail.cfm"
    print(f"STEP 2: fetching data ({data_url})")
    r2 = requests.get(data_url, headers=headers, allow_redirects=False, timeout=120)
    print(f"  data {r2.status_code}, {len(r2.content)} bytes")
    if r2.status_code != 200:
        return {"ok": False, "stage": "data", "status": r2.status_code, "body_preview": r2.text[:500]}

    # Save raw HTML for debugging — Windmill's ./shared/ persists across same-flow steps
    html_path = "./shared/completed_log_detail.html"
    import os
    os.makedirs("./shared", exist_ok=True)
    with open(html_path, "w") as f:
        f.write(r2.text)

    # STEP 3: parse via the ported parser
    print("STEP 3: parsing")
    parsed = ion_parser.parse(html_path, "service_log")
    print(
        f"  parsed: {parsed['extraction_metadata']['row_count']} rows, "
        f"{len(parsed['extraction_metadata']['profiles_found'])} profiles"
    )

    # STEP 4: normalize via mappings from app_config
    print("STEP 4: normalizing")
    normalize_result = ion_normalize.main(
        parser_output=parsed,
        supabase_connection=supabase_connection,
        write_unmapped=True,  # write unmapped back to app_config
    )

    return {
        "ok": True,
        "fetch": {
            "start_date": start_str,
            "lookback_days": lookback_days,
            "picker_bytes": len(r.content),
            "data_bytes": len(r2.content),
        },
        "parser": {
            "row_count": parsed["extraction_metadata"]["row_count"],
            "profiles_found": parsed["extraction_metadata"]["profiles_found"],
            "profile_row_counts": parsed["extraction_metadata"]["profile_row_counts"],
        },
        "normalize": normalize_result,
    }
