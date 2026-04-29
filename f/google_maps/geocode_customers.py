import requests
import psycopg2
import time
import wmill

def main():
    """
    Batch geocode active maintenance customers who are missing lat/lng.
    Uses Google Maps Geocoding API.
    Rate limit: ~10 requests/sec to stay within free tier.
    """
    api_key = wmill.get_variable("f/google_maps/api_key")
    db = wmill.get_resource("u/carter/supabase")

    conn = psycopg2.connect(
        host=db["host"],
        port=db.get("port", 6543),
        dbname=db["dbname"],
        user=db["user"],
        password=db["password"],
        sslmode="require",
    )
    cur = conn.cursor()

    # Find customers needing geocoding
    cur.execute("""
        SELECT id, street, city, state, zip
        FROM "Customers"
        WHERE is_active = true
          AND is_maintenance = true
          AND deleted_at IS NULL
          AND latitude IS NULL
          AND street IS NOT NULL
          AND city IS NOT NULL
          AND state IS NOT NULL
        ORDER BY id
        LIMIT 500
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} customers to geocode")

    geocoded = 0
    failed = 0
    errors = []

    for row in rows:
        cust_id, street, city, state, zip_code = row
        address = f"{street}, {city}, {state}"
        if zip_code:
            address += f" {zip_code}"

        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={
                    "address": address,
                    "key": api_key,
                    "components": "country:US",
                },
                timeout=10,
            )
            data = resp.json()

            if data["status"] == "OK" and data["results"]:
                loc = data["results"][0]["geometry"]["location"]
                lat = loc["lat"]
                lng = loc["lng"]

                cur.execute(
                    'UPDATE "Customers" SET latitude = %s, longitude = %s WHERE id = %s',
                    (lat, lng, cust_id),
                )
                geocoded += 1
            elif data["status"] == "ZERO_RESULTS":
                # No result — skip but don't error
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

        # Rate limit: ~10/sec
        time.sleep(0.1)

        # Commit every 50 rows
        if geocoded % 50 == 0 and geocoded > 0:
            conn.commit()
            print(f"  Progress: {geocoded} geocoded, {failed} failed")

    conn.commit()
    cur.close()
    conn.close()

    result = {
        "geocoded": geocoded,
        "failed": failed,
        "total_attempted": len(rows),
        "remaining": max(0, len(rows) - geocoded - failed),
    }
    if errors:
        result["sample_errors"] = errors[:10]

    print(f"\nDone: {geocoded} geocoded, {failed} failed out of {len(rows)} attempted")
    return result
