# f/gusto/sync_offices
#
# Standalone weekly sync of Gusto company locations -> public.branches (the office
# table), keyed by gusto_location_uuid. Keeps each office's address + coordinates
# current:
#   - active locations are upserted; geocoded (Google) when new, missing coords,
#     or the street changed. Existing branches keep their name + branch_code
#     (operational naming like "Savannah, GA" for the Garden City office); only
#     address / coords / active are refreshed.
#   - a location deactivated or removed in Gusto flips branches.active = false so
#     resolve_office() stops assigning customers to it.
#
# The employee sync (f/webhooks/get_employees) FKs employees to these branches by
# work-address location_uuid; it no longer creates branches.
#
# Triggered by: weekly schedule.
# Tables touched: public.branches [write]
# External APIs: Gusto /v1/companies/{id}/locations; Google Geocoding API.

import time
import urllib.parse
from datetime import datetime, timezone

import wmill
import requests
from supabase import create_client

GUSTO_API = "https://api.gusto.com"


def gusto_get(url, headers, max_retries=5):
    """GET with 429 backoff using the Retry-After header (default 30s)."""
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers)
        if resp.status_code != 429:
            return resp
        wait = int(resp.headers.get("Retry-After", "30"))
        print(f"429 from {url}; sleeping {wait}s (attempt {attempt + 1}/{max_retries})")
        time.sleep(wait)
    return resp


def geocode(street, city, state, zip_code, api_key):
    """Google Geocoding -> (lat, lng) or (None, None)."""
    addr = ", ".join([p for p in [street, city, state, zip_code] if p])
    if not addr:
        return None, None
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(
        {"address": addr, "key": api_key}
    )
    try:
        j = requests.get(url, timeout=20).json()
    except Exception as e:
        print(f"geocode error for {addr}: {e}")
        return None, None
    if j.get("status") == "OK" and j.get("results"):
        loc = j["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    print(f"geocode no match for {addr}: {j.get('status')}")
    return None, None


def main():
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"),
    )
    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")
    geo_key = wmill.get_variable("f/google_maps/api_key")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gusto-API-Version": "2025-06-15",
        "Accept": "application/json",
    }

    resp = gusto_get(f"{GUSTO_API}/v1/companies/{company_id}/locations", headers)
    resp.raise_for_status()
    locations = resp.json()

    existing = (
        supabase.table("branches")
        .select("id, name, gusto_location_uuid, street, latitude, longitude, active")
        .execute()
        .data
        or []
    )
    by_uuid = {b["gusto_location_uuid"]: b for b in existing if b.get("gusto_location_uuid")}

    now_iso = datetime.now(timezone.utc).isoformat()
    seen = set()
    changes = []

    for loc in locations:
        uuid = loc["uuid"]
        seen.add(uuid)
        street = loc.get("street_1") or ""
        city = loc.get("city") or ""
        state = loc.get("state") or ""
        zip_code = loc.get("zip") or ""
        active = bool(loc.get("active"))

        b = by_uuid.get(uuid)
        lat = b.get("latitude") if b else None
        lng = b.get("longitude") if b else None
        need_geo = active and (
            lat is None or lng is None or (b is not None and (b.get("street") or "") != street)
        )
        if need_geo:
            glat, glng = geocode(street, city, state, zip_code, geo_key)
            if glat is not None:
                lat, lng = glat, glng

        if b is not None:
            patch = {
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
                "active": active,
            }
            if need_geo and lat is not None:
                patch.update({"latitude": lat, "longitude": lng, "geocoded_at": now_iso})
            supabase.table("branches").update(patch).eq("id", b["id"]).execute()
            changes.append({"uuid": uuid, "action": "update", "name": b["name"], "active": active})
        elif active:
            row = {
                "name": f"{city}, {state}",
                "gusto_location_uuid": uuid,
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
                "active": True,
            }
            if lat is not None:
                row.update({"latitude": lat, "longitude": lng, "geocoded_at": now_iso})
            supabase.table("branches").insert(row).execute()
            changes.append({"uuid": uuid, "action": "insert", "name": f"{city}, {state}"})

    for uuid, b in by_uuid.items():
        if uuid not in seen and b.get("active"):
            supabase.table("branches").update({"active": False}).eq("id", b["id"]).execute()
            changes.append({"uuid": uuid, "action": "deactivate", "name": b["name"]})

    return {"gusto_locations": len(locations), "changes": changes}
