import wmill, requests
from datetime import datetime, timezone, timedelta

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"; H = {"Authorization": f"Bearer {tok}"}
    FID = "51457306"  # Joshua Francis

    # weekly assignment windows over June (endpoint caps at 7 days)
    wins, cur = [], datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    while cur < end:
        nxt = min(cur + timedelta(days=7), end)
        wins.append((cur.strftime("%Y-%m-%dT%H:%M:%SZ"), nxt.strftime("%Y-%m-%dT%H:%M:%SZ")))
        cur = nxt

    veh_ms = {}   # vehicle name -> assigned ms
    raw = []
    for s, e in wins:
        r = requests.get(f"{BASE}/fleet/vehicles/driver-assignments", headers=H,
                         params={"filterBy": "drivers", "driverIds": FID, "startTime": s, "endTime": e}, timeout=30)
        if r.status_code != 200:
            raw.append({"win": s, "status": r.status_code, "body": r.text[:200]}); continue
        for row in r.json().get("data", []):
            for a in row.get("assignments", []):
                v = a.get("vehicle", {}) or {}
                nm = v.get("name", "?")
                st = a.get("startTime"); en = a.get("endTime")
                veh_ms[nm] = veh_ms.get(nm, 0) + 1
                raw.append({"vehicle": nm, "start": st, "end": en, "isPassenger": a.get("isPassenger")})
    return {"francis_assignment_counts": veh_ms, "detail": raw[:25]}
