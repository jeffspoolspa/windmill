# ponytail: scratch — dump maintenance-truck trips for a date range (truck<->tech matching, Aug 2026 deck)
import time
from datetime import date, datetime, timedelta
import requests, wmill

API = "https://api.samsara.com"
TZ = "-04:00"

def sget(tok, path, params):
    for _ in range(5):
        r = requests.get(API + path, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5"))); continue
        return r
    return r

def main(p_start: str = "2026-08-01", p_end: str = "2026-08-31", name_filter: str = "MNT"):
    tok = wmill.get_variable("f/samsara/api_token")
    vehicles, after = [], None
    while True:
        r = sget(tok, "/fleet/vehicles", {"limit": 512, **({"after": after} if after else {})})
        r.raise_for_status()
        j = r.json()
        vehicles += [{"id": v["id"], "name": v.get("name", "")} for v in j["data"]]
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"): break
        after = pg["endCursor"]
    trucks = [v for v in vehicles if name_filter.lower() in v["name"].lower()]
    s = int(datetime.fromisoformat(f"{p_start}T00:00:00{TZ}").timestamp() * 1000)
    e = int((datetime.fromisoformat(f"{p_end}T00:00:00{TZ}") + timedelta(days=1)).timestamp() * 1000)
    out, errors = {}, {}
    for v in trucks:
        trips, cur_s = [], s
        # /v1 trips caps the window; walk it in 7-day chunks
        while cur_s < e:
            cur_e = min(cur_s + 7 * 86400 * 1000, e)
            r = sget(tok, "/v1/fleet/trips", {"vehicleId": v["id"], "startMs": cur_s, "endMs": cur_e})
            if r.status_code != 200:
                errors[v["name"]] = f"{r.status_code} {r.text[:200]}"; break
            for t in r.json().get("trips", []):
                sc, ec = t.get("startCoordinates") or {}, t.get("endCoordinates") or {}
                trips.append([t.get("startMs"), t.get("endMs"),
                              sc.get("latitude"), sc.get("longitude"),
                              ec.get("latitude"), ec.get("longitude"),
                              t.get("distanceMeters")])
            cur_s = cur_e
            time.sleep(0.2)
        out[v["name"]] = {"id": v["id"], "trips": trips}
    return {"all_vehicle_names": [v["name"] for v in vehicles], "trucks": out, "errors": errors}
