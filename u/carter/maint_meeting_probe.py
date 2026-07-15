import wmill, requests

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"; H = {"Authorization": f"Bearer {tok}"}
    P = {"startDate": "2026-06-01T00:00:00Z", "endDate": "2026-06-30T23:59:59Z"}
    out = {}

    # full vehicle table, sorted by drive proxy (find spares w/ high use)
    ve = requests.get(f"{BASE}/fleet/reports/vehicles/fuel-energy", headers=H, params=P, timeout=90).json()
    vt = []
    for v in ve.get("data", {}).get("vehicleReports", []):
        veh = v.get("vehicle", {}); run = v.get("engineRunTimeDurationMs") or 0; idle = v.get("engineIdleTimeDurationMs") or 0
        vt.append({"name": veh.get("name"), "drive_hr": round((run - idle) / 3.6e6, 1),
                   "mi": round((v.get("distanceTraveledMeters") or 0) / 1609.34)})
    out["vehicles_by_drive"] = sorted(vt, key=lambda x: -x["drive_hr"])

    # driver-vehicle assignments for Francis (try candidate endpoints)
    fid = "51457306"
    S, E = "2026-06-01T00:00:00Z", "2026-06-30T23:59:59Z"
    for nm, path, params in [
        ("assign_a", "/fleet/driver-vehicle-assignments", {"startTime": S, "endTime": E, "driverIds": fid}),
        ("assign_b", "/fleet/vehicles/driver-assignments", {"startTime": S, "endTime": E, "driverIds": fid}),
    ]:
        try:
            r = requests.get(f"{BASE}{path}", headers=H, params=params, timeout=30)
            out[nm] = {"status": r.status_code, "body": r.text[:500]}
        except Exception as e:
            out[nm] = {"error": str(e)[:150]}
    return out
