# ponytail: scratch — match maintenance trucks to techs per day (Aug 2026 deck).
# For each tech-day, score every MNT truck by the share of the tech's stops that
# a truck trip started or ended within RADIUS_M of. Best truck wins if >= MIN_SCORE.
import math, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import requests, wmill
from supabase import create_client

API = "https://api.samsara.com"
ET = timezone(timedelta(hours=-4))
RADIUS_M, MIN_SCORE = 250, 0.5

def sget(tok, path, params):
    for _ in range(5):
        r = requests.get(API + path, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5"))); continue
        return r
    return r

def dist_m(a, b):
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)

def main(p_start: str = "2026-08-01", p_end: str = "2026-08-31", name_filter: str = "MNT", detail_for: str = ""):
    tok = wmill.get_variable("f/samsara/api_token")
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"), wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))

    # roster: active Maintenance techs, Brunswick + Saint Marys
    emps = sb.table("employees").select("id,first_name,last_name,status,branches(name),departments(name)").eq("status", "active").execute().data
    roster = {e["id"]: f'{e["first_name"]} {e["last_name"]}' for e in emps
              if (e.get("departments") or {}).get("name") == "Maintenance"
              and (e.get("branches") or {}).get("name") in ("Brunswick, GA", "Saint Marys, GA")}

    # stops with coords
    vis, off = [], 0
    while True:  # PostgREST caps at 1000 rows per request
        page = sb.schema("maintenance").table("visits").select("actual_tech_id,visit_date,service_location_id") \
                 .gte("visit_date", p_start).lte("visit_date", p_end).eq("status", "completed") \
                 .in_("actual_tech_id", list(roster)).range(off, off + 999).execute().data
        vis += page
        if len(page) < 1000: break
        off += 1000
    loc_ids = list({v["service_location_id"] for v in vis if v["service_location_id"]})
    coords = {}
    for i in range(0, len(loc_ids), 200):
        for l in sb.table("service_locations").select("id,latitude,longitude").in_("id", loc_ids[i:i+200]).execute().data:
            if l["latitude"] is not None:
                coords[l["id"]] = (float(l["latitude"]), float(l["longitude"]))
    stops = defaultdict(set)  # (tech, day) -> {loc_id}
    for v in vis:
        if v["service_location_id"] in coords:
            stops[(v["actual_tech_id"], v["visit_date"])].add(v["service_location_id"])

    # trucks + trips
    vehicles, after = [], None
    while True:
        r = sget(tok, "/fleet/vehicles", {"limit": 512, **({"after": after} if after else {})})
        r.raise_for_status(); j = r.json()
        vehicles += [{"id": v["id"], "name": v.get("name", "")} for v in j["data"]]
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"): break
        after = pg["endCursor"]
    trucks = [v for v in vehicles if name_filter.lower() in v["name"].lower()]
    s = int(datetime.fromisoformat(f"{p_start}T00:00:00-04:00").timestamp() * 1000)
    e = int((datetime.fromisoformat(f"{p_end}T00:00:00-04:00") + timedelta(days=1)).timestamp() * 1000)
    pts = defaultdict(list)      # (truck, day) -> [(lat,lng)]
    drive = defaultdict(int)     # (truck, day) -> trip ms
    dist = defaultdict(int)      # (truck, day) -> meters
    errors = {}
    for v in trucks:
        cur = s
        while cur < e:
            ce = min(cur + 7 * 86400000, e)
            r = sget(tok, "/v1/fleet/trips", {"vehicleId": v["id"], "startMs": cur, "endMs": ce})
            if r.status_code != 200:
                errors[v["name"]] = f"{r.status_code} {r.text[:120]}"; break
            for t in r.json().get("trips", []):
                day = str(datetime.fromtimestamp(t["startMs"] / 1000, ET).date())
                for c in (t.get("startCoordinates"), t.get("endCoordinates")):
                    if c and c.get("latitude"):
                        pts[(v["name"], day)].append((c["latitude"], c["longitude"]))
                drive[(v["name"], day)] += (t.get("endMs") or t["startMs"]) - t["startMs"]
                dist[(v["name"], day)] += t.get("distanceMeters") or 0
            cur = ce; time.sleep(0.2)

    # score
    out = {}
    for (tid, day), locs in sorted(stops.items(), key=lambda k: (roster[k[0][0]], k[0][1])):
        best, rows = None, []
        for v in trucks:
            p = pts.get((v["name"], day))
            if not p: continue
            hit = sum(1 for l in locs if any(dist_m(coords[l], q) <= RADIUS_M for q in p))
            sc = hit / len(locs)
            rows.append((sc, v["name"]))
        rows.sort(reverse=True)
        if rows and rows[0][0] >= MIN_SCORE:
            best = rows[0][1]
        runner = rows[1] if len(rows) > 1 else None
        out.setdefault(roster[tid], []).append({
            "day": day, "stops": len(locs), "truck": best,
            "score": round(rows[0][0], 2) if rows else None,
            "runner_up": [runner[1], round(runner[0], 2)] if runner and runner[0] >= 0.3 else None,
            "drive_min": round(drive[(best, day)] / 60000) if best else None,
            "miles": round(dist[(best, day)] / 1609) if best else None})
    summary = {}
    for name, days in out.items():
        c = Counter(d["truck"] for d in days if d["truck"])
        usual = c.most_common(1)[0][0] if c else None
        summary[name] = {"usual": usual, "days": len(days),
                         "on_usual": sum(1 for d in days if d["truck"] == usual),
                         "other": [[d["day"], d["truck"], d["score"]] for d in days if d["truck"] != usual]}
    shared = defaultdict(list)
    for name, days in out.items():
        for d in days:
            if d["truck"]: shared[(d["truck"], d["day"])].append(name)
    shared = [[k[1], k[0], v] for k, v in sorted(shared.items(), key=lambda kv: (kv[0][1], kv[0][0])) if len(v) > 1]
    truck_days = {v["name"]: sum(1 for (n, d) in drive if n == v["name"]) for v in trucks}
    if detail_for:
        return {n: d for n, d in out.items() if detail_for.lower() in n.lower()}
    return {"summary": summary, "shared_truck_days": shared,
            "truck_days_moved": truck_days, "errors": errors, "geocoded_stops": len(coords)}
