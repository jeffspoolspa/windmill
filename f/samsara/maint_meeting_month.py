# f/samsara/maint_meeting_month
# Per-driver AND per-vehicle Samsara metrics for the monthly maintenance-meeting scorecard.
# Drivers join to employees via driver.externalIds.gusto (== public.employees.gusto_uuid).
# Vehicles are named for their tech (e.g. "#71 MNT-B AN, AARON N") -> name-match fallback.
# Reconcile drive-time downstream: driver-record drive primary; patch with the tech's named
# truck when the driver record is broken (assignment gap); spare-attribute the unassigned case.
#   idle:            /fleet/reports/drivers/fuel-energy -> engineIdleTimeDurationMs
#   safety + drive:  /v1/fleet/drivers/{id}/safety/score -> safetyScore, totalTimeDrivenMs
#   vehicle drive:   /fleet/reports/vehicles/fuel-energy -> engineRunTimeDurationMs - engineIdleTimeDurationMs
# Defaults to the PREVIOUS complete calendar month.
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
    sISO, eISO = start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")

    # driver-level: idle + gusto uuid
    fe = requests.get(f"{BASE}/fleet/reports/drivers/fuel-energy", headers=H,
                      params={"startDate": sISO, "endDate": eISO}, timeout=90).json()
    drivers = {}
    for d in fe.get("data", {}).get("driverReports", []):
        drv = d.get("driver", {})
        drivers[drv["id"]] = {
            "samsara_id": drv["id"], "name": drv.get("name"),
            "gusto_uuid": (drv.get("externalIds") or {}).get("gusto"),
            "idle_ms": d.get("engineIdleTimeDurationMs"),
            "run_ms": d.get("engineRunTimeDurationMs"),
        }
    # driver-level: safety + true drive time
    for sid, row in drivers.items():
        try:
            s = requests.get(f"{BASE}/v1/fleet/drivers/{sid}/safety/score", headers=H,
                             params={"startMs": startMs, "endMs": endMs}, timeout=30).json()
            row["safety"] = s.get("safetyScore")
            row["drive_ms"] = s.get("totalTimeDrivenMs")
        except Exception as e:
            row["safety_err"] = str(e)[:120]

    # vehicle-level: named truck drive proxy (run - idle) for reconciliation
    ve = requests.get(f"{BASE}/fleet/reports/vehicles/fuel-energy", headers=H,
                      params={"startDate": sISO, "endDate": eISO}, timeout=90).json()
    vehicles = []
    for v in ve.get("data", {}).get("vehicleReports", []):
        veh = v.get("vehicle", {})
        run = v.get("engineRunTimeDurationMs") or 0
        idle = v.get("engineIdleTimeDurationMs") or 0
        vehicles.append({"name": veh.get("name"), "id": veh.get("id"),
                         "idle_ms": idle, "run_ms": run, "drive_proxy_ms": run - idle,
                         "distance_m": v.get("distanceTraveledMeters")})

    return {"year": year, "month": month,
            "drivers": list(drivers.values()), "vehicles": vehicles}
