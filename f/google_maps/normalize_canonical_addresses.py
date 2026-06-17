import requests
import psycopg2
import re
import time
import wmill
from concurrent.futures import ThreadPoolExecutor


def main(limit: int = 20000):
    """ADR 005: standardize the registry's address text — re-derive street/city/state/zip
    from each row's Google place_id (canonical, never the raw input). Resumable: only touches
    rows whose street is still all-caps (`street !~ '[a-z]'`), so re-runs mop up rate-limited
    failures cheaply. Throttled (6 workers) + retries OVER_QUERY_LIMIT to stay under Google QPS."""
    api_key = wmill.get_variable("f/google_maps/api_key")
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=db["host"], port=db.get("port", 6543), dbname=db["dbname"],
                            user=db["user"], password=db["password"], sslmode="require")
    cur = conn.cursor()
    cur.execute("""select id, street, city, state, zip, place_id
                   from public.service_locations
                   where geocode_status='ok' and place_id is not null
                     and street !~ '[a-z]'
                   order by id limit %s""", (limit,))
    rows = cur.fetchall()

    def canon(row):
        loc_id, street, city, state, zc, pid = row
        for attempt in range(4):
            try:
                d = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                                 params={"place_id": pid, "key": api_key}, timeout=12).json()
            except Exception:
                time.sleep(0.4 * (attempt + 1)); continue
            s = d.get("status")
            if s == "OVER_QUERY_LIMIT":
                time.sleep(0.6 * (attempt + 1)); continue
            if s != "OK" or not d.get("results"):
                return (loc_id, None)
            comp = {t: cc for cc in d["results"][0].get("address_components", []) for t in cc["types"]}
            g = lambda t, k="long_name": comp[t][k] if t in comp else None
            m = re.match(r"^\s*(\d+)", street or "")
            num = g("street_number") or (m.group(1) if m else None)
            route = g("route")
            new_street = ((num + " " + route) if (num and route) else (route or street))
            return (loc_id, new_street,
                    g("locality") or g("postal_town") or g("sublocality") or city,
                    g("administrative_area_level_1", "short_name") or state,
                    g("postal_code") or zc,
                    (street, city, state, zc))
        return (loc_id, None)

    c = {"updated": 0, "unchanged": 0, "lookup_fail": 0}
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(canon, rows))

    for i, r in enumerate(results, 1):
        if len(r) == 2:
            c["lookup_fail"] += 1
            continue
        loc_id, st, ci, se, zc, old = r
        if (st, ci, se, zc) == old:
            c["unchanged"] += 1
            continue
        cur.execute("update public.service_locations set street=%s, city=%s, state=%s, zip=%s, updated_at=now() where id=%s",
                    (st, ci, se, zc, loc_id))
        c["updated"] += 1
        if i % 300 == 0:
            conn.commit()
    conn.commit()
    cur.close(); conn.close()
    return {"targets": len(rows), **c}
