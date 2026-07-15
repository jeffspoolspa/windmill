import wmill, requests

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"
    H = {"Authorization": f"Bearer {tok}"}
    S, E = "2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z"
    r = requests.get(f"{BASE}/fleet/drivers", headers=H, params={"limit": 5}, timeout=30)
    did = r.json()["data"][0]["id"]
    rv = requests.get(f"{BASE}/fleet/vehicles", headers=H, params={"limit": 1}, timeout=30)
    vid = rv.json()["data"][0]["id"]

    cands = [
        ("safety_events", "/fleet/safety/events", {"startTime": S, "endTime": E}),
        ("v1_safety_score", f"/v1/fleet/drivers/{did}/safety/score", {"startTime": S, "endTime": E}),
        ("veh_stats_history_engine", "/fleet/vehicles/stats/history",
             {"startTime": S, "endTime": E, "types": "engineStates"}),
        ("veh_stats_feed", "/fleet/vehicles/stats", {"types": "engineStates"}),
        ("reports_idling", "/fleet/reports/vehicle-idling", {"startTime": S, "endTime": E}),
        ("driver_efficiency", "/fleet/reports/drivers/fuel-energy", {"startTime": S, "endTime": E}),
        ("hos_clocks", "/fleet/hos/clocks", {}),
        ("trips_v1", f"/v1/fleet/trips", {"startMs": 1748736000000, "endMs": 1751328000000}),
        ("vehicle_trips", f"/fleet/vehicles/{vid}/safety/harsh-event", {}),
    ]
    out = {"driver_id": did, "vehicle_id": vid}
    for name, path, params in cands:
        try:
            rr = requests.get(f"{BASE}{path}", headers=H, params=params, timeout=30)
            out[name] = {"status": rr.status_code, "body": rr.text[:220]}
        except Exception as e:
            out[name] = {"error": str(e)[:150]}
    return out
