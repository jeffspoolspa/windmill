# requirements:
# requests
# beautifulsoup4
# psycopg2-binary

"""
f/ION/_discover/backfill_visits

Chunked historical backfill of maintenance.visits via the existing
fetch -> parse -> normalize -> upsert pipeline (f/ION/_discover/parse_normalize_test).

Why chunk: ION's CompletedLogDetail report is server-side single-state (one
primed date range at a time) and ~1.5y of logs is too big to fetch/parse/upsert
in one pass. So we loop MONTHLY windows sequentially, priming + fetching + (for
real runs) upserting each. Windows end on the 1st of the next month so adjacent
windows can't leave a gap; the upsert is idempotent so the 1-day overlap is safe.

Session: reuses the cached ION session (f/ION/session_cache variable) -- NO
chromium here. If the cache is stale the picker/data fetch returns non-200 and we
stop (refresh the cache by running the f/ION/visits flow once, then resume).

probe_only=True (default) -> fetch + parse + count only, write NOTHING. Set
probe_only=False to actually upsert the historical visits.
"""

import json
from datetime import date

import wmill
import f.ION._discover.parse_normalize_test as pnt


def _month_windows(start_month, end_month):
    """[ (YYYY-MM-01, next-month-01), ... ] inclusive of start_month..end_month."""
    y, m = map(int, start_month.split("-"))
    ey, em = map(int, end_month.split("-"))
    out = []
    while (y, m) <= (ey, em):
        start = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = date(ny, nm, 1)
        out.append((start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
        y, m = ny, nm
    return out


def main(supabase_connection, start_month="2025-01", end_month="2026-03",
         probe_only=True, write_unmapped=False):
    raw = wmill.get_variable("f/ION/session_cache")
    session = json.loads(raw)

    results = []
    totals = {"rows": 0, "bytes": 0, "visits_upserted": 0, "windows_ok": 0, "windows_failed": 0}
    for (s, e) in _month_windows(start_month, end_month):
        r = pnt.main(session, supabase_connection, start_date=s, end_date=e,
                     probe_only=probe_only, write_unmapped=write_unmapped)
        ok = bool(r.get("ok"))
        rows = (r.get("parser") or {}).get("row_count")
        byts = (r.get("fetch") or {}).get("data_bytes")
        ups = r.get("upsert") if not probe_only else None
        results.append({"window": [s, e[:7]], "ok": ok, "rows": rows,
                        "bytes": byts, "upsert": ups,
                        "stage": r.get("stage"), "status": r.get("status")})
        if ok:
            totals["windows_ok"] += 1
            totals["rows"] += rows or 0
            totals["bytes"] += byts or 0
            if isinstance(ups, dict):
                totals["visits_upserted"] += (ups.get("visits_upserted") or ups.get("visits") or 0)
        else:
            totals["windows_failed"] += 1
            break  # likely a stale session -> stop, refresh, resume

    return {"probe_only": probe_only, "start_month": start_month, "end_month": end_month,
            "windows": len(results), "totals": totals, "results": results}
