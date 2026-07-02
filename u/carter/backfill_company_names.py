import requests
import wmill
import psycopg2

# One-off (phase 2): CLEAR CompanyName in QBO for residential customers wrongly carrying one, so the
# "company filled => commercial" rule classifies them correctly. QBO is the SOURCE; Customers.company
# is a cache. Sparse update CompanyName="" then blank the cache. Auth per docs/integrations/qbo.md.
CLEAR = [
    (1994, "DEZEREAUX TRUST"),
    (6188, "POSTELL, SHANE"),
    (7380, "SPIKES, BRANDON"),
    (8533, "WILLIAMS, SUZANNE"),
]

def main():
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["dbname"], user=db["user"], password=db["password"], sslmode="require")
    cur = conn.cursor()
    cur.execute('SELECT id, qbo_customer_id FROM public."Customers" WHERE id = ANY(%s)', ([c[0] for c in CLEAR],))
    qbo_ids = {row[0]: row[1] for row in cur.fetchall()}

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
    H = {"Authorization": f"Bearer {tokens['access_token']}", "Content-Type": "application/json", "Accept": "application/json"}
    base = f"https://quickbooks.api.intuit.com/v3/company/{resource['realm_id']}"

    results = []
    cleared_ids = []
    for cust_id, name in CLEAR:
        qbo_id = qbo_ids.get(cust_id)
        if not qbo_id:
            results.append({"id": cust_id, "name": name, "error": "no qbo_customer_id"})
            continue
        read = requests.get(f"{base}/customer/{qbo_id}?minorversion=73", headers=H)
        if not read.ok:
            results.append({"id": cust_id, "name": name, "error": f"read {read.status_code}"})
            continue
        qc = read.json()["Customer"]
        body = {"Id": str(qbo_id), "SyncToken": qc["SyncToken"], "sparse": True, "CompanyName": ""}
        upd = requests.post(f"{base}/customer?minorversion=73", headers=H, json=body)
        if not upd.ok:
            results.append({"id": cust_id, "name": name, "error": f"update {upd.status_code} {upd.text[:200]}"})
            continue
        final = upd.json()["Customer"]
        now = (final.get("CompanyName") or "").strip()
        results.append({"id": cust_id, "name": name, "company_now": now or "(cleared)"})
        if not now:
            cleared_ids.append(cust_id)

    cache_cleared = 0
    if cleared_ids:
        cur.execute('UPDATE public."Customers" SET company = NULL WHERE id = ANY(%s)', (cleared_ids,))
        cache_cleared = cur.rowcount
        conn.commit()
    cur.close(); conn.close()
    return {"results": results, "cache_cleared": cache_cleared}
