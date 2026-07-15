import wmill, requests

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"
    H = {"Authorization": f"Bearer {tok}"}
    S, E = "2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z"
    out = {}

    # all drivers
    r = requests.get(f"{BASE}/fleet/drivers", headers=H, params={"limit": 512}, timeout=30)
    j = r.json()
    drivers = j.get("data", [])
    out["driver_count"] = len(drivers)
    out["driver_has_more"] = (j.get("pagination") or {}).get("hasNextPage")
    did = drivers[0]["id"] if drivers else None

    def probe(name, path, params=None):
        try:
            rr = requests.get(f"{BASE}{path}", headers=H, params=params or {}, timeout=30)
            out[name] = {"status": rr.status_code, "body": rr.text[:400]}
        except Exception as e:
            out[name] = {"error": str(e)[:200]}

    probe("safety_score", f"/fleet/drivers/{did}/safety/score", {"startTime": S, "endTime": E})
    probe("vehicles", "/fleet/vehicles", {"limit": 2})
    probe("trips", "/fleet/trips", {"startTime": S, "endTime": E})
    probe("driver_safety_events", "/fleet/drivers/safety/events", {"startTime": S, "endTime": E})
    probe("idling_report", "/fleet/reports/idling", {"startTime": S, "endTime": E})
    return out
