# f/samsara/maint_meeting_month
# Per-driver Samsara metrics for the monthly maintenance-meeting scorecard.
# Joins to employees via driver.externalIds.gusto (== public.employees.gusto_uuid).
#   - idle:   /fleet/reports/drivers/fuel-energy -> engineIdleTimeDurationMs
#   - safety + drive time: /v1/fleet/drivers/{id}/safety/score -> safetyScore, totalTimeDrivenMs
# Defaults to the PREVIOUS complete calendar month (natural for the monthly deck).
import wmill, requests, calendar
from datetime import datetime, timezone

BASE = "https://api.samsara.com"


def _bounds(year, month):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


def main(year: int = 2026, month: int = 6):
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    H = {"Authorization": f"Bearer {tok}"}
    start, end = _bounds(year, month)
    startMs, endMs = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    startISO = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    endISO = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    fe = requests.get(f"{BASE}/fleet/reports/drivers/fuel-energy", headers=H,
                      params={"startDate": startISO, "endDate": endISO}, timeout=90).json()
    rows = {}
    for d in fe.get("data", {}).get("driverReports", []):
        drv = d.get("driver", {})
        rows[drv["id"]] = {
            "samsara_id": drv["id"],
            "name": drv.get("name"),
            "gusto_uuid": (drv.get("externalIds") or {}).get("gusto"),
            "idle_ms": d.get("engineIdleTimeDurationMs"),
            "run_ms": d.get("engineRunTimeDurationMs"),
            "distance_m": d.get("distanceTraveledMeters"),
            "fuel_cost": (d.get("estFuelEnergyCost") or {}).get("amount"),
        }
    for sid, row in rows.items():
        try:
            s = requests.get(f"{BASE}/v1/fleet/drivers/{sid}/safety/score", headers=H,
                             params={"startMs": startMs, "endMs": endMs}, timeout=30).json()
            row["safety"] = s.get("safetyScore")
            row["drive_ms"] = s.get("totalTimeDrivenMs")
        except Exception as e:
            row["safety_err"] = str(e)[:120]
    return {"year": year, "month": month, "drivers": list(rows.values())}
