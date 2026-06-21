import requests
import psycopg2
import psycopg2.errors
import re
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


_ABBR = {
    "ST": "STREET", "DR": "DRIVE", "RD": "ROAD", "CT": "COURT", "LN": "LANE",
    "CIR": "CIRCLE", "BLVD": "BOULEVARD", "AVE": "AVENUE", "HWY": "HIGHWAY",
    "PKWY": "PARKWAY", "PL": "PLACE", "TRL": "TRAIL", "WY": "WAY",
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
}


def _norm(s):
    s = re.sub(r"[^A-Za-z0-9 ]", " ", (s or "").upper())
    return " ".join(_ABBR.get(t, t) for t in s.split()).strip()


def _norm_city(s):
    # City names must NOT get the street-abbrev expansion: "St Simons" would become
    # "STREET SIMONS" and fail the city-agreement guard against Google's "Saint
    # Simons Island" / "St. Simons Island". Uppercase + strip punctuation + collapse
    # whitespace only -- no _ABBR mapping.
    return " ".join(re.sub(r"[^A-Za-z0-9 ]", " ", (s or "").upper()).split())


def _ratio(a, b):
    # 1 - normalized Levenshtein distance; 1.0 == identical strings.
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


def fuzzy_resolve(api_key, street, city, state, zip_code):
    """Guarded safeguard for mistyped LEGACY addresses (new addresses should come
    from the autocomplete dropdown). The strict Geocoding API gives up to a city
    centroid on a misspelled street; Places Find Place fuzzy-matches it to a real
    address -- but it will ALSO confidently return an unrelated rooftop when the
    address truly isn't findable. So a candidate is accepted ONLY if it AGREES with
    the input: identical house number, street name within edit-distance, same city,
    and itself a precise ROOFTOP/RANGE_INTERPOLATED match. Returns a dict or None.
    """
    if not street:
        return None
    clean = re.sub(r"^(\d+)([A-Za-z])", r"\1 \2", street.strip())  # "628LONDON" -> "628 LONDON"
    query = ", ".join(p for p in [clean, city, state or "GA", zip_code] if p)
    m = re.match(r"\s*(\d+)", clean)
    in_num = m.group(1) if m else None
    in_street = _norm(re.sub(r"^\s*\d+", "", clean))
    in_city = _norm_city(city)
    try:
        fp = requests.get(
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
            params={"input": query, "inputtype": "textquery", "fields": "place_id", "key": api_key},
            timeout=10,
        ).json()
        cands = fp.get("candidates") or []
        if not cands:
            return None
        d = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"place_id": cands[0]["place_id"], "key": api_key},
            timeout=10,
        ).json()
    except Exception:
        return None
    if d.get("status") != "OK" or not d.get("results"):
        return None
    res = d["results"][0]
    if res["geometry"].get("location_type") not in ("ROOFTOP", "RANGE_INTERPOLATED"):
        return None
    comp = {t: cc for cc in res.get("address_components", []) for t in cc["types"]}

    def g(t, key="long_name"):
        return comp[t][key] if t in comp else None

    cand_num = g("street_number")
    cand_city = g("locality") or g("postal_town") or g("sublocality")
    # Agreement guard -- the corrected address must be the SAME place as the input.
    if not (in_num and cand_num and in_num == cand_num):
        return None
    if _ratio(in_street, _norm(g("route"))) < 0.72:
        return None
    cc_n = _norm_city(cand_city)
    if not (in_city and cc_n and (in_city in cc_n or cc_n in in_city or _ratio(in_city, cc_n) >= 0.7)):
        return None
    loc = res["geometry"]["location"]
    return {
        "pid": res.get("place_id"), "lat": loc["lat"], "lng": loc["lng"],
        "city": cand_city, "state": g("administrative_area_level_1", "short_name"),
        "zip": g("postal_code"),
    }


def write_precise(cur, loc_id, pid, lat, lng, ccity, cstate, czip, counters, key, source="google"):
    """Store a confirmed rooftop place_id, handling a unique(place_id) collision as
    a genuine same-building duplicate. Shared by the strict and fuzzy paths."""
    cur.execute("SAVEPOINT sp")
    try:
        cur.execute(
            """UPDATE public.service_locations SET
                 place_id=%s, place_provider='google', latitude=%s, longitude=%s,
                 geocoded_at=now(), geocode_source=%s, geocode_status='ok',
                 city=COALESCE(city,%s), state=COALESCE(state,%s), zip=COALESCE(zip,%s),
                 duplicate_of_location_id=NULL, updated_at=now()
               WHERE id=%s""",
            (pid, lat, lng, source, ccity, cstate, czip, loc_id),
        )
        cur.execute("RELEASE SAVEPOINT sp")
        counters[key] += 1
    except psycopg2.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT sp")
        cur.execute("SELECT id FROM public.service_locations WHERE place_id=%s", (pid,))
        canon = cur.fetchone()
        cur.execute(
            """UPDATE public.service_locations SET
                 geocode_status='needs_review', geocode_source=%s,
                 duplicate_of_location_id=%s, updated_at=now()
               WHERE id=%s""",
            (source, canon[0] if canon else None, loc_id),
        )
        counters["collision"] += 1


def attempt_fuzzy(cur, api_key, loc_id, street, city, state, zip_code, counters, miss_key):
    """Try the guarded fuzzy fallback; on success store the corrected rooftop,
    else leave the row place_id-NULL / needs_review for the staff dropdown."""
    fz = fuzzy_resolve(api_key, street, city, state, zip_code)
    if fz is None:
        cur.execute(
            "UPDATE public.service_locations SET geocode_status='needs_review', "
            "geocode_source='google', duplicate_of_location_id=NULL, updated_at=now() WHERE id=%s",
            (loc_id,),
        )
        counters[miss_key] += 1
    elif not in_bbox(fz["lat"], fz["lng"]):
        cur.execute(
            "UPDATE public.service_locations SET geocode_status='out_of_area', "
            "geocode_source='google_fuzzy', updated_at=now() WHERE id=%s",
            (loc_id,),
        )
        counters["out_of_area"] += 1
    else:
        write_precise(cur, loc_id, fz["pid"], fz["lat"], fz["lng"], fz["city"], fz["state"], fz["zip"],
                      counters, "fuzzy", source="google_fuzzy")


def main(limit: int = 20000, maint_only: bool = False):
    """
    Resolve active service_locations to a Google place_id + coordinate + canonical
    address (ADR 005). Server-side, resumable (only touches place_id IS NULL).

    City is REQUIRED (ADR 007). A bare street, geocoded with the service-area bounds
    bias, resolves to a SAME-NAMED street in a wrong major-GA city (a Sea Island pool
    -> "Savannah") and gets confidently stamped 'ok'. So a row with no service city is
    NOT guessed -- it's flagged 'needs_review' for manual / ION backfill. Billing is
    NOT used as a fallback hint: it's the customer's MAILING address (often a PO box in
    another town, e.g. an Eastman PO box for a Sea Island pool), which produced exactly
    these wrong pins. ION's recurring-tasks report carries the real city/ZIP; the
    reconciler lands it on the row so this geocoder has a city to trust.

    Precision gate (ADR 005): a place_id is stored ONLY for a precise match —
    location_type ROOFTOP or RANGE_INTERPOLATED and NOT partial_match. A coarse
    fallback (APPROXIMATE = city/ZIP/route centroid, whose place_id is shared by
    every un-findable address in that area) is NEVER stored; the row stays
    place_id-NULL, status 'needs_review', for correction. Invariant:
    place_id IS NOT NULL <=> geocode_status='ok'. A coarse result first gets a
    guarded Places Find Place fuzzy retry (fuzzy_resolve) for mistyped legacy
    addresses — accepted only if the corrected address agrees with the input
    (same number, fuzzy street, same city) and is itself rooftop.

    Validation: precise results are checked against SERVICE_BBOX. Out-of-area →
    flagged 'out_of_area', no coordinate. A precise place_id already held by a
    canonical row (owner change / shared address) is caught on unique(place_id),
    rolled back, flagged 'needs_review', and pointed at the canonical via
    duplicate_of_location_id (a genuine same-building duplicate). Builds the
    canonical unique-address list.
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
        SELECT sl.id, sl.street, sl.city, sl.state, sl.zip
        FROM public.service_locations sl
        WHERE sl.is_active = true AND sl.place_id IS NULL
          AND sl.street IS NOT NULL AND length(btrim(sl.street)) >= 3
          {maint_clause}
        ORDER BY sl.id
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.commit()

    c = {"ok": 0, "fuzzy": 0, "needs_review": 0, "out_of_area": 0, "collision": 0, "zero": 0, "error": 0}
    bounds = f'{SERVICE_BBOX["min_lat"]},{SERVICE_BBOX["min_lng"]}|{SERVICE_BBOX["max_lat"]},{SERVICE_BBOX["max_lng"]}'

    for i, (loc_id, s_street, s_city, s_state, s_zip) in enumerate(rows, 1):
        # City is required (ADR 007). No service city -> don't guess: a bare street
        # bounds-biases to a same-named street in a wrong major-GA city and would be
        # stamped 'ok'. Flag for manual / ION backfill instead. (No billing fallback --
        # billing is the mailing address, which caused exactly these wrong pins.)
        city = (s_city or "").strip()
        if not city:
            cur.execute(
                "UPDATE public.service_locations SET geocode_status='needs_review', "
                "geocode_source='google', duplicate_of_location_id=NULL, updated_at=now() WHERE id=%s",
                (loc_id,),
            )
            c["needs_review"] += 1
            continue
        state = s_state or "GA"
        zip_code = s_zip
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

            # A place_id is only stored when Google pinned the actual building:
            # location_type ROOFTOP or RANGE_INTERPOLATED, and NOT a partial_match.
            # Anything else is a coarse fallback (APPROXIMATE = city / ZIP / route
            # centroid) whose place_id is SHARED by every un-findable address in that
            # area. Storing it would (a) put a non-address in the canonical list and
            # (b) make unrelated bad addresses collide on unique(place_id) and look
            # like duplicates. So coarse results are never stored — the row stays
            # place_id-NULL and flagged for correction (ADR 005 invariant:
            # place_id IS NOT NULL <=> geocode_status='ok').
            is_precise = ltype in ("ROOFTOP", "RANGE_INTERPOLATED") and not partial

            if is_precise and in_bbox(lat, lng):
                write_precise(cur, loc_id, pid, lat, lng, ccity, cstate, czip, c, "ok")
            elif is_precise:
                cur.execute(
                    "UPDATE public.service_locations SET geocode_status='out_of_area', geocode_source='google', updated_at=now() WHERE id=%s",
                    (loc_id,),
                )
                c["out_of_area"] += 1
            else:
                # Strict geocode came back coarse (city/ZIP centroid) -- try the guarded
                # fuzzy fallback before flagging the row for the staff autocomplete fix.
                attempt_fuzzy(cur, api_key, loc_id, s_street, city, state, zip_code, c, "needs_review")
        elif status_api == "ZERO_RESULTS":
            attempt_fuzzy(cur, api_key, loc_id, s_street, city, state, zip_code, c, "zero")
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
