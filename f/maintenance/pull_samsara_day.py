# f/maintenance/pull_samsara_day — nightly Samsara → maintenance.samsara_driver_day
# Per driver-day: idle_ms (fuel-energy report) + drive_ms (safety score endpoint).
# Drivers keyed to employees via externalIds.gusto == employees.gusto_uuid.
# Defaults pull yesterday (America/New_York); pass p_start/p_end for backfills.
import time
from datetime import date, datetime, timedelta, timezone

import requests
import wmill
from supabase import create_client

TZ = "-04:00"  # ET offset used for day boundaries; drift to -05:00 in winter is
               # a one-hour edge on day boundaries — fine for daily rollups.
API = "https://api.samsara.com"


def day_bounds_ms(d: date):
    start = datetime.fromisoformat(f"{d}T00:00:00{TZ}")
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def sget(tok, path, params, tries=4):
    for i in range(tries):
        r = requests.get(API + path, headers={"Authorization": f"Bearer {tok}"},
                         params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        return r
    return r


def main(p_start: str = "2026-07-01", p_end: str = "2026-08-10"):  # ponytail: temp backfill defaults; revert to "" after
    tok = wmill.get_variable("f/samsara/api_token")
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"),
                       wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))

    if not p_start:
        y = (datetime.now(timezone(timedelta(hours=-4))) - timedelta(days=1)).date()
        p_start = p_end = str(y)
    start, end = date.fromisoformat(p_start), date.fromisoformat(p_end or p_start)

    # gusto_uuid -> employees.id
    emp = {e["gusto_uuid"]: e["id"]
           for e in sb.table("employees").select("id,gusto_uuid")
                      .not_.is_("gusto_uuid", "null").execute().data}

    rows, unmatched, d = [], set(), start
    while d <= end:
        r = sget(tok, "/fleet/reports/drivers/fuel-energy",
                 {"startDate": f"{d}T00:00:00{TZ}", "endDate": f"{d}T23:59:59{TZ}"})
        r.raise_for_status()
        s_ms, e_ms = day_bounds_ms(d)
        for rep in r.json().get("data", {}).get("driverReports", []):
            drv = rep.get("driver", {})
            guuid = (drv.get("externalIds") or {}).get("gusto")
            if not guuid or guuid not in emp:
                if drv.get("name"):
                    unmatched.add(drv["name"])
                continue
            drive_ms = None
            sc = sget(tok, f"/v1/fleet/drivers/{drv['id']}/safety/score",
                      {"startMs": s_ms, "endMs": e_ms})
            if sc.status_code == 200:
                drive_ms = sc.json().get("totalTimeDrivenMs")
            rows.append({"employee_id": emp[guuid], "day": str(d),
                         "drive_ms": drive_ms,
                         "idle_ms": rep.get("engineIdleTimeDurationMs"),
                         "samsara_driver_id": str(drv["id"]),
                         "updated_at": datetime.now(timezone.utc).isoformat()})
            time.sleep(0.2)
        d += timedelta(days=1)

    for i in range(0, len(rows), 200):
        sb.schema("maintenance").table("samsara_driver_day") \
          .upsert(rows[i:i + 200]).execute()
    return {"days": (end - start).days + 1, "rows": len(rows),
            "unmatched_drivers": sorted(unmatched)}
