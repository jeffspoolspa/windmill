import requests
import psycopg2
import time
import re
import wmill

BRUNSWICK_ZIPS = [
    "31520", "31521", "31522", "31523", "31524", "31525",
    "31527", "31561", "31568", "31548", "31558", "31565", "31569",
]
RICHMOND_HILL_ZIPS = [
    "31324", "31328", "31405", "31406", "31407", "31408", "31409",
    "31410", "31411", "31412", "31414", "31415", "31416", "31419",
    "31421", "31302", "31312", "31313", "31314", "31315", "31316",
    "31320", "31321", "31323", "31326", "31327", "31329",
    "31301", "31305", "31309", "31319", "31331", "31333",
]
SERVICE_AREA_ZIPS = set(BRUNSWICK_ZIPS + RICHMOND_HILL_ZIPS)

def in_service_area(zip_code):
    if not zip_code:
        return False
    z = zip_code.strip()[:5]
    if z in SERVICE_AREA_ZIPS:
        return True
    if z.startswith("31") and z.isdigit() and 31300 <= int(z) <= 31599:
        return True
    return False

def is_valid_street(street):
    if not street:
        return False
    s = street.strip()
    if len(s) < 3:
        return False
    if not re.match(r'^\d+\s', s):
        return False
    if s.isdigit():
        return False
    return True


def main():
    api_key = wmill.get_variable("f/google_maps/api_key")
    db = wmill.get_resource("u/carter/supabase")

    conn = psycopg2.connect(
        host=db["host"], port=db.get("port", 6543),
        dbname=db["dbname"], user=db["user"],
        password=db["password"], sslmode="require",
    )
    cur = conn.cursor()

    # Only fetch customers that haven't been normalized yet (no lat/lng)
    cur.execute("""
        SELECT id, display_name,
            service_street, service_city, service_state, service_zip,
            street, city, state, zip
        FROM "Customers"
        WHERE is_active = true
          AND deleted_at IS NULL
          AND latitude IS NULL
        ORDER BY id
    """)
    all_rows = cur.fetchall()
    print(f"Found {len(all_rows)} active customers needing normalization")

    # Validate and filter
    valid = []
    skipped_no_address = 0
    skipped_bad_street = 0
    skipped_out_of_area = 0
    skipped_bad_street_samples = []

    for row in all_rows:
        cust_id, name = row[0], row[1]
        s_street, s_city, s_state, s_zip = row[2], row[3], row[4], row[5]
        b_street, b_city, b_state, b_zip = row[6], row[7], row[8], row[9]

        street = s_street or b_street
        city = s_city or b_city
        state = s_state or b_state
        zip_code = s_zip or b_zip

        if not street or not city or not state:
            skipped_no_address += 1
            continue

        if not is_valid_street(street):
            skipped_bad_street += 1
            if len(skipped_bad_street_samples) < 20:
                skipped_bad_street_samples.append(f"ID {cust_id} ({name}): '{street}'")
            continue

        if not in_service_area(zip_code):
            skipped_out_of_area += 1
            continue

        valid.append((cust_id, street, city, state, zip_code))

    print(f"\nValidation:")
    print(f"  Valid for geocoding: {len(valid)}")
    print(f"  Skipped no address: {skipped_no_address}")
    print(f"  Skipped bad street: {skipped_bad_street}")
    print(f"  Skipped out of area: {skipped_out_of_area}")

    BATCH_LIMIT = 2000
    to_process = valid[:BATCH_LIMIT]
    print(f"\nGeocoding {len(to_process)} customers...")

    updated = 0
    failed = 0
    errors = []

    for cust_id, street, city, state, zip_code in to_process:
        address = f"{street}, {city}, {state} {zip_code or ''}".strip()

        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": address, "key": api_key, "components": "country:US"},
                timeout=10,
            )
            data = resp.json()

            if data["status"] == "OK" and data["results"]:
                result = data["results"][0]
                lat = result["geometry"]["location"]["lat"]
                lng = result["geometry"]["location"]["lng"]

                components = result.get("address_components", [])
                def get_comp(types, name_type="long_name"):
                    for comp in components:
                        if any(t in comp["types"] for t in types):
                            return comp[name_type]
                    return None

                street_number = get_comp(["street_number"]) or ""
                route = get_comp(["route"]) or ""
                canonical_street = f"{street_number} {route}".strip() or street
                canonical_city = get_comp(["locality"]) or get_comp(["sublocality"]) or city
                canonical_state = get_comp(["administrative_area_level_1"], "short_name") or state
                canonical_zip = get_comp(["postal_code"]) or zip_code

                if not in_service_area(canonical_zip):
                    errors.append(f"ID {cust_id}: Geocoded to out-of-area ZIP {canonical_zip}")
                    failed += 1
                    time.sleep(0.1)
                    continue

                cur.execute(
                    """UPDATE "Customers"
                       SET latitude = %s, longitude = %s,
                           service_street = %s, service_city = %s,
                           service_state = %s, service_zip = %s
                       WHERE id = %s""",
                    (lat, lng, canonical_street, canonical_city,
                     canonical_state, canonical_zip, cust_id),
                )
                updated += 1
            elif data["status"] == "ZERO_RESULTS":
                errors.append(f"ID {cust_id}: No results for '{address}'")
                failed += 1
            else:
                errors.append(f"ID {cust_id}: API status {data['status']}")
                failed += 1
                if data["status"] in ("OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
                    print("Rate limit hit — stopping")
                    break
        except Exception as e:
            errors.append(f"ID {cust_id}: {type(e).__name__}: {str(e)[:100]}")
            failed += 1

        time.sleep(0.1)

        if updated % 100 == 0 and updated > 0:
            conn.commit()
            print(f"  Progress: {updated} normalized, {failed} failed")

    conn.commit()
    cur.close()
    conn.close()

    result = {
        "updated": updated,
        "failed": failed,
        "batch_size": len(to_process),
        "remaining_valid": max(0, len(valid) - BATCH_LIMIT),
        "remaining_unnormalized": len(all_rows) - updated,
        "skipped_no_address": skipped_no_address,
        "skipped_bad_street": skipped_bad_street,
        "skipped_out_of_area": skipped_out_of_area,
    }
    if errors:
        result["sample_errors"] = errors[:15]
    if skipped_bad_street_samples:
        result["sample_bad_streets"] = skipped_bad_street_samples

    print(f"\nDone: {updated} normalized, {failed} failed")
    print(f"Remaining valid to process: {max(0, len(valid) - BATCH_LIMIT)}")
    return result
