# extra_requirements:
# psycopg[binary]
# wmill
# Land Samsara facts into the warehouse, raw as they arrived: samsara.stop / leg / vehicle (engineStates + GPS per
# truck-day). No business rules and no driver identity: which tech drove which truck comes from Core, by matching
# stops to visited addresses (core.match_truck_days).
# Windmill path: f/warehouse/land_samsara.py; only main() touches Windmill.
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import requests

API = "https://api.samsara.com"
ET = ZoneInfo("America/New_York")  # day boundaries are company-local days
MIN_STOP_MIN = 2
MERGE_M, MERGE_S = 100, 180  # same spot + engine on < 3 min = one stop (on-site reposition)


def upsert(cur, table, key, rows, batch=500):
    """One INSERT ... ON CONFLICT per batch; rows are dicts with the same keys."""
    if not rows:
        return
    cols = list(rows[0])
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key)
    sql = (f"insert into {table} ({', '.join(cols)}) select {', '.join(cols)} "
           f"from jsonb_populate_recordset(null::{table}, %s::jsonb) "
           f"on conflict ({', '.join(key)}) do update set {sets}")
    for i in range(0, len(rows), batch):
        cur.execute(sql, [json.dumps(rows[i:i + batch], default=str)])


def sget(tok, path, params, tries=5):
    for _ in range(tries):
        r = requests.get(API + path, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=60)
        if r.status_code != 429:
            return r
        time.sleep(int(r.headers.get("Retry-After", "5")))
    return r


def days(start, end):
    d, e = date.fromisoformat(str(start)), date.fromisoformat(str(end or start))
    while d <= e:
        yield d
        d += timedelta(days=1)


def bounds(d):
    """Local midnight and the next local midnight (DST-correct)."""
    s = datetime(d.year, d.month, d.day, tzinfo=ET)
    n = d + timedelta(days=1)
    return s, datetime(n.year, n.month, n.day, tzinfo=ET)


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0])))


def hist(tok, vid, d, types):
    s, e = bounds(d)
    out, after = [], None
    base = {"vehicleIds": vid, "startTime": s.isoformat(), "endTime": (e - timedelta(seconds=1)).isoformat(),
            "types": types}
    while True:
        r = sget(tok, "/fleet/vehicles/stats/history", {**base, **({"after": after} if after else {})})
        r.raise_for_status()
        j = r.json()
        for veh in j.get("data", []):
            out += veh.get(types, [])
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"):
            return out
        after = pg["endCursor"]


def build_stops(es, gps):
    """engineStates [(dt, 'On'|'Off'|'Idle')] + gps [(dt, lat, lng, addr, geofence)] -> merged stops, or None."""
    if not gps or not any(v == "On" for _, v in es):
        return None
    def fix_at(t): return min(gps, key=lambda g: abs((g[0] - t).total_seconds()))

    k = next(n for n, x in enumerate(es) if x[1] == "On")
    f = fix_at(es[k][0])  # overnight park: midnight -> first engine-on
    stops = [{"arrive": es[k][0].replace(hour=0, minute=0, second=0, microsecond=0), "depart": es[k][0],
              "open": False, "overnight": True, "idle": 0.0, "lat": f[1], "lng": f[2], "addr": f[3], "geo": f[4]}]
    i = k
    while i < len(es):
        if es[i][1] == "On":
            i += 1
            continue
        j, idle = i, 0.0
        while j < len(es) and es[j][1] != "On":
            t1 = es[j + 1][0] if j + 1 < len(es) else es[j][0]
            if es[j][1] == "Idle":
                idle += (t1 - es[j][0]).total_seconds() / 60
            j += 1
        t0, open_end = es[i][0], j >= len(es)
        t1 = t0 if open_end else es[j][0]
        if open_end or (t1 - t0).total_seconds() / 60 >= MIN_STOP_MIN:
            f = fix_at(t0)
            stops.append({"arrive": t0, "depart": t1, "open": open_end, "overnight": False, "idle": idle,
                          "lat": f[1], "lng": f[2], "addr": f[3], "geo": f[4]})
        i = j
    merged = []
    for st in stops:
        m = merged[-1] if merged else None
        if m and not m["overnight"] and dist_m((m["lat"], m["lng"]), (st["lat"], st["lng"])) <= MERGE_M \
                and (st["arrive"] - m["depart"]).total_seconds() < MERGE_S:
            m["depart"], m["idle"], m["open"] = st["depart"], m["idle"] + st["idle"], st["open"]
        else:
            merged.append(st)
    return merged


def truck_day(tok, v, d):
    """-> (stop rows, leg rows) for one vehicle-day, or None if the truck never ran."""
    def loc(ts): return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    es = [(loc(x["time"]), x["value"]) for x in hist(tok, v["id"], d, "engineStates")]
    if not any(val == "On" for _, val in es):
        return None
    gps = [(loc(g["time"]), g["latitude"], g["longitude"], (g.get("reverseGeo") or {}).get("formattedLocation"),
            (g.get("address") or {}).get("name")) for g in hist(tok, v["id"], d, "gps")]
    merged = build_stops(es, gps)
    if merged is None:
        return None
    now, day = datetime.now(timezone.utc).isoformat(), str(d)
    rows_s = [{"vehicle_id": v["id"], "vehicle_name": v["name"], "day": day, "stop": n + 1,
               "arrive": st["arrive"].isoformat(), "depart": None if st["open"] else st["depart"].isoformat(),
               "min": None if (st["open"] or st["overnight"]) else round((st["depart"] - st["arrive"]).total_seconds() / 60, 1),
               "idle_min": round(st["idle"], 1), "lat": st["lat"], "lng": st["lng"], "address": st["addr"],
               "geofence": st["geo"], "updated_at": now} for n, st in enumerate(merged)]
    rows_l = []
    for n, (a, b) in enumerate(zip(merged, merged[1:])):
        path = [g for g in gps if a["depart"] <= g[0] <= b["arrive"]]
        mi = sum(dist_m(path[q][1:3], path[q + 1][1:3]) for q in range(len(path) - 1)) / 1609
        rows_l.append({"vehicle_id": v["id"], "day": day, "leg": n + 1, "from_stop": n + 1, "to_stop": n + 2,
                       "depart": a["depart"].isoformat(), "arrive": b["arrive"].isoformat(),
                       "min": round((b["arrive"] - a["depart"]).total_seconds() / 60, 1), "mi": round(mi, 1),
                       "updated_at": now})
    return rows_s, rows_l


def land_truck_days(db_url, tok, start, end=None, name_filter="MNT|Spare|Savannah"):
    """All vehicles -> samsara.vehicle; per matching truck-day, stops + legs -> samsara.stop / samsara.leg.
    Each landed truck-day replaces its previous rows, so a rerun with fewer stops leaves no stale tail."""
    vehicles, after = [], None
    while True:
        r = sget(tok, "/fleet/vehicles", {"limit": 512, **({"after": after} if after else {})})
        r.raise_for_status()
        j = r.json()
        vehicles += [{"vehicle_id": v["id"], "name": v.get("name", ""), "vin": v.get("vin"), "make": v.get("make"),
                      "model": v.get("model"), "year": v.get("year")} for v in j["data"]]
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"):
            break
        after = pg["endCursor"]
    now = datetime.now(timezone.utc).isoformat()
    for v in vehicles:
        v["updated_at"] = now
    pats = [p.lower() for p in name_filter.split("|") if p]
    trucks = [{"id": v["vehicle_id"], "name": v["name"]} for v in vehicles
              if not pats or any(p in v["name"].lower() for p in pats)]

    stops, legs, done, errors = [], [], [], []
    for v in trucks:
        for d in days(start, end):
            try:
                res = truck_day(tok, v, d)
                done.append({"vehicle_id": v["id"], "day": str(d)})
                if res:
                    stops += res[0]
                    legs += res[1]
            except Exception as ex:  # keep going; report
                errors.append([v["name"], str(d), str(ex)[:120]])
            time.sleep(0.1)

    with psycopg.connect(db_url) as c, c.cursor() as cur:  # one transaction
        upsert(cur, "samsara.vehicle", ("vehicle_id",), vehicles)
        for t in ("samsara.stop", "samsara.leg"):
            cur.execute(f"delete from {t} x using jsonb_to_recordset(%s::jsonb) k(vehicle_id text, day date) "
                        "where x.vehicle_id = k.vehicle_id and x.day = k.day", [json.dumps(done)])
        upsert(cur, "samsara.stop", ("vehicle_id", "day", "stop"), stops)
        upsert(cur, "samsara.leg", ("vehicle_id", "day", "leg"), legs)
    return {"vehicles": len(vehicles), "trucks": len(trucks), "truck_days": len(done),
            "stops": len(stops), "legs": len(legs), "errors": errors}


def main(kind: str, start: str = "", end: str = "", name_filter: str = "MNT|Spare|Savannah"):
    """kind: 'truck_days'. start/end are local dates; default yesterday."""
    import wmill
    db, tok = wmill.get_variable("f/warehouse/land_url"), wmill.get_variable("f/samsara/api_token").strip()
    start = start or str(datetime.now(ET).date() - timedelta(days=1))
    end = end or start
    if kind == "truck_days":
        return land_truck_days(db, tok, start, end, name_filter)
    raise ValueError(f"unknown kind {kind!r}")


if __name__ == "__main__":  # stop-builder self-check, no network or database
    t = lambda h, m: datetime(2026, 9, 2, h, m, tzinfo=ET)
    es = [(t(6, 59), "Off"), (t(7, 0), "On"), (t(7, 20), "Off"), (t(7, 50), "On"), (t(8, 0), "Idle"),
          (t(8, 1), "On"), (t(16, 0), "Off")]
    gps = [(t(7, 0), 31.0, -81.0, "shop", None), (t(7, 20), 31.1, -81.1, "pool", None),
           (t(16, 0), 31.0, -81.0, "shop", None)]
    s = build_stops(es, gps)
    assert [x["addr"] for x in s] == ["shop", "pool", "shop"], s
    assert s[0]["overnight"] and s[0]["arrive"].hour == 0 and s[-1]["open"], s
    b0, b1 = bounds(date(2026, 11, 1))  # DST ends: a 25-hour day
    assert b1.timestamp() - b0.timestamp() == 25 * 3600
    print("ok")
