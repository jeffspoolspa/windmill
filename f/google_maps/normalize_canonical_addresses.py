import requests
import psycopg2
import re
import wmill
from concurrent.futures import ThreadPoolExecutor


def main(limit: int = 20000):
    """ADR 005: standardize the registry's address text. Every 'ok' row has a place_id,
    so re-derive street/city/state/zip from Google's CANONICAL components (never the raw
    legacy input) so the whole entity has one uniform format ("168 Zellwood Dr", not a mix
    of "168 ZELLWOOD DRIVE" / "159 ZELLWOOD DR" / "179 Zellwood Drive")."""
    api_key = wmill.get_variable("f/google_maps/api_key")
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=db["host"], port=db.get("port", 6543), dbname=db["dbname"],
                            user=db["user"], password=db["password"], sslmode="require")
    cur = conn.cursor()
    cur.execute("""select id, street, city, state, zip, place_id
                   from public.service_locations
                   where geocode_status='ok' and place_id is not null
                   order by id limit %s""", (limit,))
    rows = cur.fetchall()

    def canon(row):
        loc_id, street, city, state, zc, pid = row
        try:
            d = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                             params={"place_id": pid, "key": api_key}, timeout=12).json()
        except Exception:
            return (loc_id, None)
        if d.get("status") != "OK" or not d.get("results"):
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

    c = {"updated": 0, "unchanged": 0, "lookup_fail": 0}
    with ThreadPoolExecutor(max_workers=10) as ex:
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
