import requests
import wmill
import psycopg2

def main(billing_month: str = "2026-02", dry_run: bool = True):
    """
    Refresh QBO token ONCE for the entire flow and create a billing_run record.
    Returns access_token + realm_id for all downstream modules to reuse.
    """
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)

    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"])
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.text}")

    tokens = response.json()
    access_token = tokens["access_token"]
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    realm_id = resource["realm_id"]

    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"]
    )
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO billing.billing_runs (billing_month, status, started_at)
            VALUES (%s, 'autopay_processing', now())
            ON CONFLICT (billing_month) DO UPDATE SET
                status = 'autopay_processing',
                started_at = now(),
                updated_at = now()
            RETURNING id
        """, (billing_month,))
        billing_run_id = str(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    return {
        "access_token": access_token,
        "realm_id": realm_id,
        "billing_run_id": billing_run_id
    }
