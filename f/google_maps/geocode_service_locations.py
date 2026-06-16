import requests
import psycopg2
import psycopg2.errors
import time
import wmill

# Service-area bbox — keep in sync with app/(shell)/maintenance/_lib/geo.ts SERVICE_BBOX.
SERVICE_BBOX = {"min_lat": 30.2, "max_lat": 32.7, "min_lng": -82.4, "max_lng": -80.6}


def in_bbox(lat, lng):
    return (
        lat is not None
        and SERVICE_BBOX["min_lat"] <= lat <= SERVICE_BBOX["max_lat"]
        and SERVICE_BBOX["min_lng"] <= lng <= SERVICE_BBOX["max_lng"]
    )


def main(limit: int = 20000, maint_only: bool = False):
    """
    Resolve active service_locations to a Google place_id + coordinate + canonical
    address (ADR 005). Resumable (place_id IS NULL). Street-only rows use the
    customer's BILLING city/state/zip as the geocode hint; the CANONICAL components
    from Google are stored. Out-of-area → flagged, no coord. place_id collisions →
    flagged 'needs_review' + duplicate_of_location_id pointed at the canonical row.
    """
    api_key = wmill.get_variable("f/google_maps/api_key")
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db.get("port", 6543), dbname=db["dbname"],
        user=db["user"], password=db["password"], sslmode="require",
    )
    cur = conn.cursor()

    maint_clause = (
        "AND EXISTS (SELECT 1 FROM maintenance.tasks t WHERE t.service_location_id = sl.id)"
        if maint_only else ""
    )
    cur.execute(f"""
        SELECT sl.id, sl.street, sl.city, sl.state, sl.zip,
               c.city, c.state, c.zip
        FROM public.service_locations sl
        JOIN public."Customers" c ON c.id = sl.account_id
        WHERE sl.is_active = true AND sl.place_id IS NULL
          AND sl.street IS NOT NULL AND length(btrim(sl.street)) >= 3
          {maint_clause}
        ORDER BY sl.id
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.commit()

    c = {"ok": 0, "needs_review": 0, "out_of_area": 0, "collision": 0, "zero": 0, "error": 0}
    bounds = f'{SERVICE_BBOX["min_lat"]},{SERVICE_BBOX["min_lng"]}|{SERVICE_BBOX["max_lat"]},{SERVICE_BBOX["max_lng"]}'

    for i, (loc_id, s_street, s_city, s_state, s_zip, b_city, b_state, b_zip) in enumerate(rows, 1):
        city = s_city or b_city
        state = s_state or b_state or "GA"
        zip_code = s_zip or b_zip
        parts = [s_street]
        if city:
            parts.append(city)
        parts.append(state)
        if zip_code:
            parts.append(zip_code)
        address = ", ".join(p for p in parts if p)

        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": address, "key": api_key, "components": "country:US", "bounds": bounds},
                timeout=10,
            )
            data = resp.json()
        except Exception:
            c["error"] += 1
            time.sleep(0.1)
            continue

        status_api = data.get("status")
        if status_api in ("OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
            print("Rate limit hit — stopping")
            break

        if status_api == "OK" and data.get("results"):
            res = data["results"][0]
            loc = res["geometry"]["location"]
            lat, lng = loc["lat"], loc["lng"]
            pid = res.get("place_id")
            ltype = res["geometry"].get("location_type")
            partial = res.get("partial_match", False)
            comp = {t: cc for cc in res.get("address_components", []) for t in cc["types"]}

            def g(t, key="long_name"):
                return comp[t][key] if t in comp else None

            ccity = g("locality") or g("postal_town") or g("sublocality")
            cstate = g("administrative_area_level_1", "short_name")
            czip = g("postal_code")

            if not in_bbox(lat, lng):
                cur.execute(
                    "UPDATE public.service_locations SET geocode_status='out_of_area', geocode_source='google', updated_at=now() WHERE id=%s",
                    (loc_id,),
                )
                c["out_of_area"] += 1
            else:
                row_status = "ok" if (ltype in ("ROOFTOP", "RANGE_INTERPOLATED") and not partial) else "needs_review"
                cur.execute("SAVEPOINT sp")
                try:
                    cur.execute(
                        """UPDATE public.service_locations SET
                             place_id=%s, place_provider='google', latitude=%s, longitude=%s,
                             geocoded_at=now(), geocode_source='google', geocode_status=%s,
                             city=COALESCE(city,%s), state=COALESCE(state,%s), zip=COALESCE(zip,%s),
                             duplicate_of_location_id=NULL, updated_at=now()
                           WHERE id=%s""",
                        (pid, lat, lng, row_status, ccity, cstate, czip, loc_id),
                    )
                    cur.execute("RELEASE SAVEPOINT sp")
                    c[row_status] += 1
                except psycopg2.errors.UniqueViolation:
                    cur.execute("ROLLBACK TO SAVEPOINT sp")
                    cur.execute("SELECT id FROM public.service_locations WHERE place_id=%s", (pid,))
                    canon = cur.fetchone()
                    cur.execute(
                        """UPDATE public.service_locations SET
                             geocode_status='needs_review', geocode_source='google',
                             duplicate_of_location_id=%s, updated_at=now()
                           WHERE id=%s""",
                        (canon[0] if canon else None, loc_id),
                    )
                    c["collision"] += 1
        elif status_api == "ZERO_RESULTS":
            cur.execute(
                "UPDATE public.service_locations SET geocode_status='needs_review', geocode_source='google', updated_at=now() WHERE id=%s",
                (loc_id,),
            )
            c["zero"] += 1
        else:
            c["error"] += 1

        time.sleep(0.08)
        if i % 100 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}  {c}")

    conn.commit()
    cur.close()
    conn.close()
    result = {"total_targets": len(rows), **c}
    print(f"DONE: {result}")
    return result
