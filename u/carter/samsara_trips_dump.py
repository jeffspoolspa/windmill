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

def main(p_start: str = "2026-08-01", p_end: str = "2026-08-31", name_filter: str = "MNT", detail_for: str = "", compact: bool = False, probe_day: str = "", stops_for: str = "", day_log: str = "", loc_day: str = "", gps_truck: str = "", gps_from: str = "", gps_to: str = "", gps_near: str = "", raw: bool = False):
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
        page = sb.schema("maintenance").table("visits").select("id,actual_tech_id,visit_date,service_location_id,started_at,ended_at") \
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
    trips = defaultdict(list)    # (truck, day) -> [(startMs,endMs,endlat,endlng)]
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
                ec = t.get("endCoordinates") or {}
                if ec.get("latitude"):
                    trips[(v["name"], day)].append((t["startMs"], t.get("endMs") or t["startMs"], ec["latitude"], ec["longitude"], t.get("distanceMeters") or 0))
                drive[(v["name"], day)] += (t.get("endMs") or t["startMs"]) - t["startMs"]
                dist[(v["name"], day)] += t.get("distanceMeters") or 0
            cur = ce; time.sleep(0.2)

    if raw:  # untouched sample of both Samsara payloads for one truck (first MNT match), one day
        v = trucks[0]
        s0 = int(datetime.fromisoformat(f"{p_start}T00:00:00-04:00").timestamp() * 1000)
        tr = sget(tok, "/v1/fleet/trips", {"vehicleId": v["id"], "startMs": s0, "endMs": s0 + 86400000}).json()
        g = sget(tok, "/fleet/vehicles/stats/history", {"vehicleIds": v["id"], "types": "gps",
                 "startTime": f"{p_start}T10:40:00-04:00", "endTime": f"{p_start}T10:42:00-04:00"}).json()
        veh = sget(tok, "/fleet/vehicles", {"limit": 1}).json()
        return {"vehicle": v, "trips_response_keys": list(tr.keys()), "trip_count": len(tr.get("trips", [])),
                "trip_sample": tr.get("trips", [])[:2],
                "gps_response_keys": list(g.keys()), "gps_pagination": g.get("pagination"),
                "gps_sample": {k: (val[:3] if k == "gps" else val) for k, val in (g.get("data") or [{}])[0].items()},
                "vehicles_sample": veh}
    if gps_truck:  # raw GPS breadcrumbs for one truck in a window; distance to a lat,lng if given
        v = next(x for x in trucks if gps_truck.lower() in x["name"].lower())
        r = sget(tok, "/fleet/vehicles/stats/history", {"vehicleIds": v["id"], "types": "gps",
                 "startTime": gps_from, "endTime": gps_to})
        r.raise_for_status()
        near = tuple(map(float, gps_near.split(","))) if gps_near else None
        out = []
        for veh in r.json().get("data", []):
            for g in veh.get("gps", []):
                q = (g["latitude"], g["longitude"])
                out.append([g["time"][11:19], round(g.get("speedMilesPerHour", 0)), round(dist_m(near, q)) if near else None,
                            round(q[0], 5), round(q[1], 5)])
        return {"truck": v["name"], "points": out}
    if loc_day and stops_for:  # LOCATION-FIRST: cluster parks by place, match to route pools by distance only
        tech_ids = [t for t, n in roster.items() if stops_for.lower() in n.lower()]
        locs = set()
        for tid in tech_ids: locs |= stops.get((tid, loc_day), set())
        best, bs = None, 0
        for v in trucks:
            p = pts.get((v["name"], loc_day))
            if not p: continue
            sc = sum(1 for l in locs if any(dist_m(coords[l], q) <= RADIUS_M for q in p)) / max(len(locs), 1)
            if sc > bs: best, bs = v["name"], sc
        def fmt(ms): return datetime.fromtimestamp(ms / 1000, ET).strftime("%H:%M")
        branches = [(float(b["latitude"]), float(b["longitude"])) for b in sb.table("branches").select("latitude,longitude").execute().data if b["latitude"]]
        tl = sorted(trips[(best, loc_day)]) if best else []
        # parks = gap after each trip except the last; cluster by 100 m
        clusters = []  # {"c":(lat,lng), "parks":[(parkMs, leaveMs)], "trip_idx":[i]}
        for i, t in enumerate(tl):
            if i + 1 >= len(tl): break
            q = (t[2], t[3])
            for c in clusters:
                if dist_m(c["c"], q) <= 100:
                    c["parks"].append((t[1], tl[i + 1][0])); c["trip_idx"].append(i); break
            else:
                clusters.append({"c": q, "parks": [(t[1], tl[i + 1][0])], "trip_idx": [i]})
        # match route pools to nearest cluster within 300 m (distance only)
        pool_stop = {}
        for l in locs:
            near = min(((dist_m(coords[l], c["c"]), k) for k, c in enumerate(clusters)), default=None)
            if near and near[0] <= 300: pool_stop[l] = (near[1], near[0])
        stops_out = []
        for k, c in enumerate(clusters):
            pools = [l for l, (kk, _) in pool_stop.items() if kk == k]
            kind = "route pool" if pools else "office" if any(dist_m(b, c["c"]) <= 300 for b in branches) else "non-route"
            stops_out.append({"stop": k + 1, "coords": [round(c["c"][0], 5), round(c["c"][1], 5)], "kind": kind,
                              "pools": pools, "parked": [f"{fmt(a)}-{fmt(b)}" for a, b in c["parks"]],
                              "total_min": round(sum(b - a for a, b in c["parks"]) / 60000)})
        # legs: trip i goes from cluster-of-(i-1) to cluster-of-i
        at = {}
        for k, c in enumerate(clusters):
            for i in c["trip_idx"]: at[i] = k + 1
        legs = [{"leg": i + 1, "from": at.get(i - 1, "start"), "to": at.get(i, "end"),
                 "drive": f"{fmt(t[0])}-{fmt(t[1])}", "min": round((t[1] - t[0]) / 60000), "mi": round(t[4] / 1609, 1)} for i, t in enumerate(tl)]
        visits_out = []
        for v in sorted(vis, key=lambda v: v["started_at"] or ""):
            if v["actual_tech_id"] in tech_ids and v["visit_date"] == loc_day:
                l = v["service_location_id"]
                ps = pool_stop.get(l)
                visits_out.append({"loc": l, "ion": f"{(v['started_at'] or '')[11:16]}-{(v['ended_at'] or '')[11:16]}",
                                   "stop": ps[0] + 1 if ps else None, "m_from_pin": ps[1] if ps else None})
        return {"truck": best, "score": round(bs, 2), "stops": stops_out, "legs": legs, "visits": visits_out,
                "unmatched_visits": [x["loc"] for x in visits_out if x["stop"] is None],
                "non_route_stops": [x["stop"] for x in stops_out if x["kind"] == "non-route"]}
    if day_log and stops_for:  # one tech-day: ION stops vs the truck's full trip/park log
        tech_ids = [t for t, n in roster.items() if stops_for.lower() in n.lower()]
        locs = set()
        for tid in tech_ids: locs |= stops.get((tid, day_log), set())
        best, bs = None, 0
        for v in trucks:
            p = pts.get((v["name"], day_log))
            if not p: continue
            sc = sum(1 for l in locs if any(dist_m(coords[l], q) <= RADIUS_M for q in p)) / max(len(locs), 1)
            if sc > bs: best, bs = v["name"], sc
        def fmt(ms): return datetime.fromtimestamp(ms / 1000, ET).strftime("%H:%M")
        tl = sorted(trips[(best, day_log)]) if best else []
        log = []
        for i, t in enumerate(tl):
            near = min(((round(dist_m(coords[l], (t[2], t[3]))), l) for l in locs), default=None)
            park = round((tl[i + 1][0] - t[1]) / 60000) if i + 1 < len(tl) else None
            log.append({"trip": f"{fmt(t[0])}-{fmt(t[1])}", "ends_at": [round(t[2], 5), round(t[3], 5)],
                        "parked_min": park, "nearest_route_pool_m": near})
        ion = []
        for v in vis:
            if v["actual_tech_id"] in tech_ids and v["visit_date"] == day_log:
                l = v["service_location_id"]
                ion.append({"loc": l, "coords": coords.get(l), "start": (v["started_at"] or "")[11:16], "end": (v["ended_at"] or "")[11:16]})
        return {"truck": best, "score": round(bs, 2), "ion": sorted(ion, key=lambda x: x["start"]), "truck_log": log}
    if stops_for:
        # dwell = gap between consecutive trips of the tech's matched truck, parked at trip_k end
        tech_ids = [t for t, n in roster.items() if stops_for.lower() in n.lower()]
        branches = [(float(b["latitude"]), float(b["longitude"])) for b in sb.table("branches").select("latitude,longitude").execute().data if b["latitude"]]
        all_locs, off = [], 0
        while True:
            pg = sb.table("service_locations").select("latitude,longitude").not_.is_("latitude", "null").range(off, off + 999).execute().data
            all_locs += [(float(l["latitude"]), float(l["longitude"])) for l in pg]
            if len(pg) < 1000: break
            off += 1000
        def fmt(ms): return datetime.fromtimestamp(ms / 1000, ET).strftime("%H:%M")
        def wall(ts):  # ION started_at/ended_at = ET wall time mislabeled UTC
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=ET) if ts else None
        visits_out, nonpool, geo_suspect, summ = [], [], [], {"logged_min": 0, "dwell_min": 0, "matched": 0, "unmatched": 0, "nonpool_min": 0, "nonpool_stops": 0}
        days = sorted({v["visit_date"] for v in vis if v["actual_tech_id"] in tech_ids})
        for day in days:
            locs = set()
            for tid in tech_ids: locs |= stops.get((tid, day), set())
            if not locs: continue
            # best truck for the day (same scoring as main mode)
            best, bs = None, 0
            for v in trucks:
                p = pts.get((v["name"], day))
                if not p: continue
                sc = sum(1 for l in locs if any(dist_m(coords[l], q) <= RADIUS_M for q in p)) / len(locs)
                if sc > bs: best, bs = v["name"], sc
            if not best or bs < MIN_SCORE: continue
            tl = sorted(trips[(best, day)])
            dwells = [(tl[i][1], tl[i + 1][0], tl[i][2], tl[i][3]) for i in range(len(tl) - 1)]  # (parkMs, leaveMs, lat, lng)
            used = {}
            # group visit rows per location (multi-body sites = one stop)
            per_loc = defaultdict(list)
            for v in vis:
                if v["actual_tech_id"] in tech_ids and v["visit_date"] == day and v["service_location_id"] in locs:
                    per_loc[v["service_location_id"]].append(v)
            for l, rows in sorted(per_loc.items(), key=lambda kv: min((r["started_at"] or "9") for r in kv[1])):
                st = [wall(r["started_at"]) for r in rows if r["started_at"]]
                en = [wall(r["ended_at"]) for r in rows if r["ended_at"]]
                lst, len_ = (min(st) if st else None), (max(en) if en else None)
                logged = round((len_ - lst).total_seconds() / 60) if lst and len_ else None
                # a park may serve two pools within 150 m of each other (tech walks) -> not consumed
                cands = [(i, d) for i, d in enumerate(dwells) if dist_m(coords[l], (d[2], d[3])) <= 300
                         and (i not in used or any(dist_m(coords[l], coords[o]) <= 150 for o in used[i]))]
                how = "geo"
                if cands and lst and len_:  # prefer the park that overlaps the ION window, then nearest in time
                    def ov(c): return min(c[1][1] / 1000, len_.timestamp()) - max(c[1][0] / 1000, lst.timestamp())
                    i, d = max(cands, key=lambda c: (ov(c) > 0, ov(c), -abs(c[1][0] / 1000 - lst.timestamp())))
                elif cands and lst:
                    i, d = min(cands, key=lambda c: abs(c[1][0] / 1000 - lst.timestamp()))
                elif cands:
                    i, d = cands[0]
                elif lst and len_:  # geocode suspect: unclaimed park overlapping the ION window
                    ov = [(min(d[1] / 1000, len_.timestamp()) - max(d[0] / 1000, lst.timestamp()), i, d)
                          for i, d in enumerate(dwells) if i not in used]
                    ov = [o for o in ov if o[0] >= 300]  # >= 5 min overlap
                    if ov:
                        _, i, d = max(ov); how = "time"
                        geo_suspect.append([day, l, round(dist_m(coords[l], (d[2], d[3]))), round(d[2], 5), round(d[3], 5)])
                    else:
                        i, d = None, None
                else:
                    i, d = None, None
                if d:
                    used.setdefault(i, []).append(l); dm = round((d[1] - d[0]) / 60000)
                    summ["matched"] += 1; summ["dwell_min"] += dm
                    if logged: summ["logged_min"] += logged
                    visits_out.append([day, l, lst.strftime("%H:%M") if lst else None, logged, fmt(d[0]), dm, len(rows), how])
                else:
                    summ["unmatched"] += 1
                    visits_out.append([day, l, lst.strftime("%H:%M") if lst else None, logged, None, None, len(rows), None])
            for i, d in enumerate(dwells):
                dm = round((d[1] - d[0]) / 60000)
                if i in used or dm < 5: continue
                q = (d[2], d[3])
                if any(dist_m(coords[l], q) <= 300 for l in locs): continue  # near a route pool, just unmatched
                kind = "shop" if any(dist_m(b, q) <= 300 for b in branches) else \
                       "customer" if any(dist_m(a, q) <= 120 for a in all_locs) else "other"
                nonpool.append([day, fmt(d[0]), dm, kind, round(q[0], 5), round(q[1], 5)])
                summ["nonpool_min"] += dm; summ["nonpool_stops"] += 1
        return {"tech": stops_for, "summary": summ, "visits": visits_out, "nonpool": nonpool, "geo_suspect": geo_suspect}
    if probe_day:  # per stop: nearest trip endpoint from ANY fetched vehicle that day
        res = {}
        for (tid, day), locs in stops.items():
            if day != probe_day or detail_for.lower() not in roster[tid].lower(): continue
            for l in locs:
                best = None
                for v in trucks:
                    for q in pts.get((v["name"], day), []):
                        dm = dist_m(coords[l], q)
                        if best is None or dm < best[0]: best = (round(dm), v["name"])
                res[f"{coords[l][0]:.5f},{coords[l][1]:.5f}"] = best
        return {"tech_day": f"{detail_for} {probe_day}", "stops": res,
                "vehicles_with_trips_that_day": sorted({n for (n, d) in pts if d == probe_day})}
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
    if compact:
        return [[n, d["day"], d["truck"], d["drive_min"], d["miles"], d["score"]] for n, days in out.items() for d in days]
    if detail_for:
        return {n: d for n, d in out.items() if detail_for.lower() in n.lower()}
    return {"summary": summary, "shared_truck_days": shared,
            "truck_days_moved": truck_days, "errors": errors, "geocoded_stops": len(coords)}
