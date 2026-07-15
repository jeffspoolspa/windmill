import wmill, requests
from datetime import datetime, timezone

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"; H = {"Authorization": f"Bearer {tok}"}
    startMs = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    endMs = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
    did = requests.get(f"{BASE}/fleet/drivers", headers=H, params={"limit": 1}, timeout=30).json()["data"][0]["id"]
    out = {"did": did}
    r = requests.get(f"{BASE}/v1/fleet/drivers/{did}/safety/score", headers=H,
                     params={"startMs": startMs, "endMs": endMs}, timeout=30)
    out["safety"] = {"status": r.status_code, "body": r.text[:600]}
    for nm, path in [("drv_fuel", "/fleet/reports/drivers/fuel-energy"),
                     ("veh_fuel", "/fleet/reports/vehicles/fuel-energy")]:
        rr = requests.get(f"{BASE}{path}", headers=H,
                          params={"startDate": "2026-06-01", "endDate": "2026-06-30"}, timeout=45)
        out[nm] = {"status": rr.status_code, "body": rr.text[:600]}
    return out
