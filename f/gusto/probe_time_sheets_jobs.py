"""
READ-ONLY probe: does Gusto's time_tracking/time_sheets endpoint return
job/project allocation for this company (which is on NATIVE time tracking)?

Purpose: the Gusto MCP wrapper's list_time_records returns source='native'
with `shifts` objects that carry NO job/project field. The CSV export of the
"time tracking hours" report DOES have a Job column. This probe determines
whether the REST API can surface job/project per shift.

Safety:
  - Uses f/gusto/personal_access_token (does NOT rotate).
  - Deliberately does NOT touch f/gusto/refresh_token, which rotates on every
    use and is shared with f/webhooks/get_employees + workers_comp.
  - GET only. No writes to Gusto. No writes to Windmill variables.

Returns the raw response shape so we can inspect fields rather than guess.
"""
import json
import urllib.parse
import urllib.request

import wmill

GUSTO_BASE = "https://api.gusto.com"
API_VERSION = "2026-06-15"


def _get(url: str, token: str):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    req.add_header("X-Gusto-API-Version", API_VERSION)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return {
                "ok": True,
                "status": resp.status,
                "body": json.loads(body) if body.strip() else None,
            }
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        return {"ok": False, "status": e.code, "error": detail}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "error": repr(e)}


def _shape(obj, depth=0):
    """Summarize structure without dumping PII-heavy full payloads."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


def main(
    start_date: str = "2026-06-01",
    end_date: str = "2026-06-30",
    per: int = 100,
):
    token = wmill.get_variable("f/gusto/personal_access_token")
    company_id = wmill.get_variable("f/gusto/company_id")

    results = {}

    # --- Probe 1: the documented time_sheets collection -------------------
    ts_url = (
        f"{GUSTO_BASE}/v1/companies/{company_id}/time_tracking/time_sheets"
        + "?"
        + urllib.parse.urlencode({"entity_type": "Employee", "per": per})
    )
    ts = _get(ts_url, token)
    results["time_sheets"] = {
        "url": ts_url,
        "status": ts.get("status"),
        "ok": ts.get("ok"),
    }
    if ts.get("ok"):
        body = ts["body"]
        sheets = body if isinstance(body, list) else (body or {}).get("time_sheets", body)
        count = len(sheets) if isinstance(sheets, list) else None
        results["time_sheets"]["count"] = count
        results["time_sheets"]["shape"] = _shape(sheets)
        if isinstance(sheets, list) and sheets:
            first = sheets[0]
            results["time_sheets"]["first_keys"] = sorted(first.keys()) if isinstance(first, dict) else None
            # hunt for anything job/project shaped anywhere in the record
            blob = json.dumps(sheets).lower()
            results["time_sheets"]["mentions_job"] = "job" in blob
            results["time_sheets"]["mentions_project"] = "project" in blob
            results["time_sheets"]["sample_record"] = first
    else:
        results["time_sheets"]["error"] = ts.get("error")

    # --- Probe 2: token scope check --------------------------------------
    ti = _get(f"{GUSTO_BASE}/v1/token_info", token)
    results["token_info"] = {
        "status": ti.get("status"),
        "ok": ti.get("ok"),
        "body": ti.get("body") if ti.get("ok") else ti.get("error"),
    }

    return results
