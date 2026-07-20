# f/ops/audit_script_usage
#
# Daily per-script Windmill usage rollup -> ops.script_usage_daily.
#
# For a given UTC calendar day, pages the Windmill jobs API (cursor pagination
# on created_before — the `page` param is IGNORED by this endpoint, so paging
# by page number silently returns the same first page forever), aggregates by
# script_path (runs, summed duration, failures, trigger-source breakdown), and
# upserts one row per script for that day. Idempotent: re-running a day
# replaces it.
#
# runs = execution count (what Windmill bills). compute_s = summed duration.
# Auth: WM_TOKEN (job-scoped, injected by the runtime) for the jobs API;
# u/carter/supabase for the write.

import os
import json
from datetime import datetime, timedelta, timezone

import psycopg2
import requests
import wmill

SUPABASE_RESOURCE = "u/carter/supabase"


def _conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def _classify(created_by: str, has_path: bool) -> str:
    cb = created_by or ""
    if cb.startswith("schedule-"):
        return "schedule"
    if cb.startswith("webhook"):
        return "webhook"
    if cb == "label-local":
        return "wake"          # pg_net wake POST
    if cb.startswith("email-"):
        return "email"
    if not has_path:
        return "preview"
    return "manual"            # a person / API run by path


def _fetch_day(day_str: str) -> dict:
    base = os.environ.get("BASE_INTERNAL_URL") or "https://app.windmill.dev"
    token = os.environ["WM_TOKEN"]
    ws = os.environ.get("WM_WORKSPACE", "jps-internal")
    after = f"{day_str}T00:00:00Z"
    day_end = f"{day_str}T23:59:59.999Z"
    h = {"Authorization": f"Bearer {token}"}

    agg, seen = {}, set()
    cursor = None
    pages = 0
    while pages < 2000:                       # guard: ~2M jobs/day ceiling
        pages += 1
        params = {"created_after": after,
                  "created_before": cursor or day_end,
                  "per_page": 1000}
        r = requests.get(f"{base}/api/w/{ws}/jobs/list",
                         params=params, headers=h, timeout=120)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        new = 0
        oldest = None
        for j in rows:
            ca = j.get("created_at")
            if ca and (oldest is None or ca < oldest):
                oldest = ca
            jid = j.get("id")
            if jid in seen:
                continue
            seen.add(jid)
            new += 1
            path = j.get("script_path") or f"<{j.get('job_kind', '?')}>"
            a = agg.setdefault(path, {"runs": 0, "ms": 0, "failed": 0, "kinds": {}})
            a["runs"] += 1
            a["ms"] += (j.get("duration_ms") or 0)
            if j.get("success") is False:
                a["failed"] += 1
            k = _classify(j.get("created_by"), bool(j.get("script_path")))
            a["kinds"][k] = a["kinds"].get(k, 0) + 1
        if new == 0:            # cursor stopped advancing -> done
            break
        cursor = oldest
    return agg


def main(day: str = ""):
    """Roll up one UTC calendar day. Default: yesterday."""
    if not day:
        day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    agg = _fetch_day(day)

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM ops.script_usage_daily WHERE day = %s", (day,))
        for path, a in agg.items():
            cur.execute(
                """INSERT INTO ops.script_usage_daily
                     (day, script_path, runs, compute_s, failed, kinds)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (day, path, a["runs"], round(a["ms"] / 1000, 1),
                 a["failed"], json.dumps(a["kinds"])),
            )
        conn.commit()
    finally:
        conn.close()

    total = sum(a["runs"] for a in agg.values())
    top = sorted(agg.items(), key=lambda kv: kv[1]["runs"], reverse=True)[:5]
    return {
        "day": day,
        "distinct_scripts": len(agg),
        "total_runs": total,
        "top5": [{"script": p, "runs": a["runs"]} for p, a in top],
    }
