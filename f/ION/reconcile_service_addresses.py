# requirements:
# psycopg2-binary

"""
f/ION/reconcile_service_addresses

Make ION's recurring-tasks report the LEADER for a service_location's city/state/zip
(ADR 007). service_locations are born street-only from the visits ingester
(f/ION/_lib/upsert) and nothing ever lands a city on them; the geocoder used to fill
the gap by bounds-biasing the bare street to a same-named street in a wrong major-GA
city (a Sea Island pool -> "Savannah") and stamping it 'ok'. ION actually carries the
right city/ZIP (ion.recurring_tasks, keyed by service_location_id) -- this reconciler
lands it, so the (now city-required) geocoder has a real city to trust.

Two cases, both keyed on ion.recurring_tasks -> service_locations.id:

  A. FILL    -- service_location has no city but ION does: copy ION's city/state/zip
               into the NULL slots. Lets the geocoder resolve (or, still city-less,
               flag needs_review) instead of guessing.
  B. CORRECT -- the stored address is in the WRONG AREA: its ZIP region (first 3) differs
               from ION's AND the stored city is a genuinely different city from ION's
               (normalized, non-substring -- so "ST SIMONS" vs "St. Simons Island" does
               NOT count). Overwrite city/state/zip from ION, drop the wrong place_id +
               coordinate, and flag needs_review so the geocoder re-pins with the right
               city. The city-must-also-differ guard avoids ION's own ZIP anomalies
               (e.g. a Townsend row tagged ZIP 31522): if the city already agrees, a
               lone ZIP-region difference is left alone.

Idempotent: once a row's ZIP region matches ION, case B never fires again -- no thrash.
Runs on the ingestion cadence (schedule), so new street-only rows get ION's city before
the geocoder sees them.

SAFETY: defaults to dry_run=True (rolls back after capturing rowcounts + examples).

Public API:
    main(dry_run=True) -> stats
"""

import psycopg2
import wmill

_FILL_SQL = """
WITH ion_truth AS (
  SELECT service_location_id,
         (array_agg(initcap(city) ORDER BY (city IS NULL OR city=''), synced_at DESC))[1]  AS ion_city,
         (array_agg(upper(state)  ORDER BY (state IS NULL OR state=''), synced_at DESC))[1] AS ion_state,
         (array_agg(zip           ORDER BY (zip  IS NULL OR zip=''),  synced_at DESC))[1]   AS ion_zip
  FROM ion.recurring_tasks
  WHERE service_location_id IS NOT NULL
  GROUP BY service_location_id
)
UPDATE public.service_locations sl
   SET city  = COALESCE(NULLIF(sl.city,''),  it.ion_city),
       state = COALESCE(NULLIF(sl.state,''), NULLIF(it.ion_state,''), 'GA'),
       zip   = COALESCE(NULLIF(sl.zip,''),   it.ion_zip),
       updated_at = now()
  FROM ion_truth it
 WHERE it.service_location_id = sl.id
   AND sl.is_active
   AND (sl.city IS NULL OR sl.city = '')
   AND it.ion_city IS NOT NULL AND it.ion_city <> ''
RETURNING sl.id
"""

_CORRECT_SQL = """
WITH ion_truth AS (
  SELECT service_location_id,
         (array_agg(initcap(city) ORDER BY (city IS NULL OR city=''), synced_at DESC))[1]  AS ion_city,
         (array_agg(upper(state)  ORDER BY (state IS NULL OR state=''), synced_at DESC))[1] AS ion_state,
         (array_agg(zip           ORDER BY (zip  IS NULL OR zip=''),  synced_at DESC))[1]   AS ion_zip
  FROM ion.recurring_tasks
  WHERE service_location_id IS NOT NULL
  GROUP BY service_location_id
)
UPDATE public.service_locations sl
   SET city  = it.ion_city,
       state = COALESCE(NULLIF(it.ion_state,''), 'GA'),
       zip   = it.ion_zip,
       place_id = NULL, latitude = NULL, longitude = NULL,
       geocode_status = 'needs_review', geocode_source = 'ion_reconcile',
       duplicate_of_location_id = NULL, updated_at = now()
  FROM ion_truth it
 WHERE it.service_location_id = sl.id
   AND sl.is_active
   AND it.ion_zip ~ '^[0-9]{5}' AND sl.zip ~ '^[0-9]{5}'
   AND left(sl.zip,3) <> left(it.ion_zip,3)
   AND regexp_replace(upper(coalesce(it.ion_city,'')),'[^A-Z0-9]','','g') <> ''
   AND position(regexp_replace(upper(coalesce(it.ion_city,'')),'[^A-Z0-9]','','g')
                in regexp_replace(upper(coalesce(sl.city,'')),'[^A-Z0-9]','','g')) = 0
   AND position(regexp_replace(upper(coalesce(sl.city,'')),'[^A-Z0-9]','','g')
                in regexp_replace(upper(coalesce(it.ion_city,'')),'[^A-Z0-9]','','g')) = 0
RETURNING sl.id, sl.city, sl.zip
"""


def main(dry_run: bool = True) -> dict:
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db.get("port", 6543), dbname=db["dbname"],
        user=db["user"], password=db["password"], sslmode="require",
    )
    try:
        cur = conn.cursor()
        cur.execute(_CORRECT_SQL)
        corrected = cur.fetchall()
        cur.execute(_FILL_SQL)
        filled = cur.fetchall()
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {
            "dry_run": dry_run, "committed": not dry_run,
            "corrected_count": len(corrected), "filled_count": len(filled),
            "corrected_examples": [
                {"location_id": r[0], "new_city": r[1], "new_zip": r[2]} for r in corrected[:30]
            ],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
