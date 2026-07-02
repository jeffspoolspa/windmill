import requests
import wmill
import psycopg2

# One-off backfill: 12 commercial customers (HOAs, clubs, property cos) have no CompanyName in QBO,
# so the "company filled => commercial" rule misses them. QBO is the SOURCE of company; our
# Customers.company is a cache. For each: read QBO customer (SyncToken), sparse-update
# CompanyName = DisplayName, then set the cache. Auth per docs/integrations/qbo.md: refresh once,
# PERSIST the rotated refresh token, reuse the access token. Sequential (no concurrent QBO writes).
TARGETS = [
    (790, "8308", "Bradley Pt. South"),
    (1007, "9200", "BULL RIVER YACHT CLUB"),
    (2550, "8436", "Forest Lakes"),
    (2903, "8452", "Governors Quarters HOA"),
    (3439, "8472", "Highlands"),
    (3440, "8473", "Highlands Falls"),
    (4672, "8576", "Live Oak"),
    (6429, "8050", "RESERVE AT DEMERE"),
    (6510, "8687", "Richmond Place HOA"),
    (6822, "8711", "Sanctuary HOA"),
    (7168, "9298", "SJC PROPERTIES"),
    (8375, "9145", "WEXFORD HOA"),
]

def main(dry_run: bool = False):
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)
    r = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not r.ok:
        raise Exception(f"Token refresh failed: {r.status_code} - {r.text}")
    tokens = r.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(value=resource, path=resource_path)  # persist rotation (required)
    access_token = tokens["access_token"]
    realm_id = resource["realm_id"]
    H = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Accept": "application/json"}
    base = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"

    results = []
    updated_ids = []
    for cust_id, qbo_id, name in TARGETS:
        read = requests.get(f"{base}/customer/{qbo_id}?minorversion=73", headers=H)
        if not read.ok:
            results.append({"id": cust_id, "name": name, "error": f"read {read.status_code}"})
            continue
        qc = read.json()["Customer"]
        current = (qc.get("CompanyName") or "").strip()
        if current:
            results.append({"id": cust_id, "name": name, "skipped": f"already has CompanyName '{current}'"})
            updated_ids.append((cust_id, current))
            continue
        if dry_run:
            results.append({"id": cust_id, "name": name, "would_set": qc["DisplayName"]})
            continue
        body = {"Id": qbo_id, "SyncToken": qc["SyncToken"], "sparse": True, "CompanyName": qc["DisplayName"]}
        upd = requests.post(f"{base}/customer?minorversion=73", headers=H, json=body)
        if not upd.ok:
            results.append({"id": cust_id, "name": name, "error": f"update {upd.status_code} {upd.text[:200]}"})
            continue
        final = upd.json()["Customer"]
        results.append({"id": cust_id, "name": name, "set": final.get("CompanyName")})
        updated_ids.append((cust_id, final.get("CompanyName")))

    cache_updated = 0
    if not dry_run and updated_ids:
        db = wmill.get_resource("u/carter/supabase")
        conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["dbname"], user=db["user"], password=db["password"], sslmode="require")
        cur = conn.cursor()
        for cust_id, company in updated_ids:
            cur.execute('UPDATE public."Customers" SET company = %s WHERE id = %s', (company, cust_id))
            cache_updated += cur.rowcount
        conn.commit()
        cur.close(); conn.close()
    return {"dry_run": dry_run, "results": results, "cache_updated": cache_updated}
