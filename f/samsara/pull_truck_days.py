# f/samsara/pull_truck_days — Samsara layer ONLY: per truck-day, stops + legs from
# engineStates + GPS history -> maintenance.samsara_stops / samsara_legs.
# No techs, no business rules; the mapping step (SQL) runs over these tables.
# Stop = contiguous Off/Idle span >= MIN_STOP_MIN at one place (idle tracked per stop);
# overnight park = arrive midnight / depart first engine-on; final park = depart NULL.
# Defaults pull yesterday (America/New_York); pass p_start/p_end for backfills.
import math, time
from datetime import datetime, timedelta, timezone

import requests
import wmill
from supabase import create_client

API = "https://api.samsara.com"
ET = timezone(timedelta(hours=-4))  # ponytail: fixed offset; DST edge = 1h on day boundaries
MIN_STOP_MIN = 2
MERGE_M, MERGE_S = 100, 180  # same spot + engine on < 3 min = one stop (on-site reposition)


def sget(tok, path, params):
    for _ in range(5):
        r = requests.get(API + path, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5"))); continue
        return r
    return r


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0])))


def hist(tok, vid, day, types):
    out, after = [], None
    base = {"vehicleIds": vid, "startTime": f"{day}T00:00:00-04:00", "endTime": f"{day}T23:59:59-04:00", "types": types}
    while True:
        r = sget(tok, "/fleet/vehicles/stats/history", {**base, **({"after": after} if after else {})})
        r.raise_for_status(); j = r.json()
        for veh in j.get("data", []):
            out += veh.get(types, [])
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"):
            return out
        after = pg["endCursor"]


def truck_day(tok, v, day):
    """-> (stop rows, leg rows) for one vehicle-day, or None if the truck never ran."""
    def et(ts): return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    es = [(et(x["time"]), x["value"]) for x in hist(tok, v["id"], day, "engineStates")]
    if not any(val == "On" for _, val in es):
        return None
    gps = [(et(g["time"]), g["latitude"], g["longitude"], (g.get("reverseGeo") or {}).get("formattedLocation"),
            (g.get("address") or {}).get("name")) for g in hist(tok, v["id"], day, "gps")]
    if not gps:
        return None
    def fix_at(t): return min(gps, key=lambda g: abs((g[0] - t).total_seconds()))

    stops = []
    k = next(n for n, x in enumerate(es) if x[1] == "On")
    f = fix_at(es[k][0])  # overnight park (Samsara emits a 0-length Off right before the first On)
    stops.append({"arrive": es[k][0].replace(hour=0, minute=0, second=0, microsecond=0), "depart": es[k][0], "open": False,
                  "overnight": True, "idle": 0.0, "lat": f[1], "lng": f[2], "addr": f[3], "geo": f[4]})
    i = k
    while i < len(es):
        if es[i][1] == "On":
            i += 1; continue
        j, idle = i, 0.0
        while j < len(es) and es[j][1] != "On":
            t1 = es[j + 1][0] if j + 1 < len(es) else es[j][0]
            if es[j][1] == "Idle":
                idle += (t1 - es[j][0]).total_seconds() / 60
            j += 1
        t0 = es[i][0]; open_end = j >= len(es); t1 = es[j][0] if not open_end else t0
        if (t1 - t0).total_seconds() / 60 >= MIN_STOP_MIN or open_end:
            f = fix_at(t0)
            stops.append({"arrive": t0, "depart": t1, "open": open_end, "overnight": False, "idle": idle,
                          "lat": f[1], "lng": f[2], "addr": f[3], "geo": f[4]})
        i = j
    merged = []
    for st in stops:
        m = merged[-1] if merged else None
        if m and not m["overnight"] and dist_m((m["lat"], m["lng"]), (st["lat"], st["lng"])) <= MERGE_M \
                and (st["arrive"] - m["depart"]).total_seconds() < MERGE_S:
            m["depart"] = st["depart"]; m["idle"] += st["idle"]; m["open"] = st["open"]
        else:
            merged.append(st)
    now = datetime.now(timezone.utc).isoformat()
    rows_s = [{"vehicle_id": v["id"], "vehicle_name": v["name"], "day": day, "stop": n + 1,
               "arrive": st["arrive"].isoformat(), "depart": None if st["open"] else st["depart"].isoformat(),
               "min": None if (st["open"] or st["overnight"]) else round((st["depart"] - st["arrive"]).total_seconds() / 60, 1),
               "idle_min": round(st["idle"], 1), "lat": st["lat"], "lng": st["lng"], "address": st["addr"], "geofence": st["geo"],
               "updated_at": now} for n, st in enumerate(merged)]
    rows_l = []
    for n, (a, b) in enumerate(zip(merged, merged[1:])):
        path = [g for g in gps if a["depart"] <= g[0] <= b["arrive"]]
        mi = sum(dist_m(path[q][1:3], path[q + 1][1:3]) for q in range(len(path) - 1)) / 1609
        rows_l.append({"vehicle_id": v["id"], "day": day, "leg": n + 1, "from_stop": n + 1, "to_stop": n + 2,
                       "depart": a["depart"].isoformat(), "arrive": b["arrive"].isoformat(),
                       "min": round((b["arrive"] - a["depart"]).total_seconds() / 60, 1), "mi": round(mi, 1), "updated_at": now})
    return rows_s, rows_l


def main(p_start: str = "", p_end: str = "", name_filter: str = "MNT|Spare"):
    tok = wmill.get_variable("f/samsara/api_token")
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"), wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))
    if not p_start:
        p_start = p_end = str((datetime.now(ET) - timedelta(days=1)).date())
    p_end = p_end or p_start
    vehicles, after = [], None
    while True:
        r = sget(tok, "/fleet/vehicles", {"limit": 512, **({"after": after} if after else {})})
        r.raise_for_status(); j = r.json()
        vehicles += [{"id": v["id"], "name": v.get("name", ""), "vin": v.get("vin"), "make": v.get("make"),
                      "model": v.get("model"), "year": v.get("year")} for v in j["data"]]
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"):
            break
        after = pg["endCursor"]
    sb.schema("maintenance").table("samsara_vehicles").upsert([
        {"vehicle_id": v["id"], "name": v["name"], "vin": v["vin"], "make": v["make"], "model": v["model"], "year": v["year"],
         "updated_at": datetime.now(timezone.utc).isoformat()} for v in vehicles]).execute()
    pats = [p.lower() for p in name_filter.split("|") if p]
    trucks = [v for v in vehicles if not pats or any(p in v["name"].lower() for p in pats)]
    d0, d1 = datetime.fromisoformat(p_start).date(), datetime.fromisoformat(p_end).date()
    written, errors = 0, []
    for v in trucks:
        d = d0
        while d <= d1:
            try:
                res = truck_day(tok, v, str(d))
                if res:
                    rows_s, rows_l = res
                    # ponytail: upsert on PK only; a rerun with fewer stops leaves stale tail rows
                    sb.schema("maintenance").table("samsara_stops").upsert(rows_s).execute()
                    if rows_l:
                        sb.schema("maintenance").table("samsara_legs").upsert(rows_l).execute()
                    written += 1
            except Exception as ex:  # keep going; report
                errors.append([v["name"], str(d), str(ex)[:120]])
            d += timedelta(days=1)
            time.sleep(0.1)
    return {"trucks": len(trucks), "truck_days_written": written, "errors": errors}
