import wmill, requests

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"; H = {"Authorization": f"Bearer {tok}"}
    P = {"startDate": "2026-06-01T00:00:00Z", "endDate": "2026-06-30T23:59:59Z"}

    ve = requests.get(f"{BASE}/fleet/reports/vehicles/fuel-energy", headers=H, params=P, timeout=90).json()
    vehs = []
    for v in ve.get("data", {}).get("vehicleReports", []):
        veh = v.get("vehicle", {})
        run = v.get("engineRunTimeDurationMs") or 0
        idle = v.get("engineIdleTimeDurationMs") or 0
        vehs.append({
            "name": veh.get("name"), "id": veh.get("id"),
            "idle_hr": round(idle / 3.6e6, 1), "run_hr": round(run / 3.6e6, 1),
            "drive_hr_proxy": round((run - idle) / 3.6e6, 1),
            "dist_mi": round((v.get("distanceTraveledMeters") or 0) / 1609.34),
        })

    dr = requests.get(f"{BASE}/fleet/drivers", headers=H, params={"limit": 512}, timeout=30).json()
    drivers = [{"id": d["id"], "name": d.get("name"),
                "gusto": (d.get("externalIds") or {}).get("gusto")} for d in dr.get("data", [])]

    def hit(s, keys):
        s = (s or "").upper()
        return any(k in s for k in keys)

    keys = ["FRANCIS", "ELMORE", "CARROLL", "DELMORE", "JF", "JC"]
    return {
        "vehicles_of_interest": [v for v in vehs if hit(v["name"], keys)],
        "all_vehicle_names": [v["name"] for v in vehs],
        "drivers_of_interest": [d for d in drivers if hit(d["name"], ["FRANCIS", "ELMORE", "CARROLL"])],
        "driver_count": len(drivers),
    }
