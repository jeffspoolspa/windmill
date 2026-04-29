
import requests
import wmill
import psycopg2
import psycopg2.extras

def main(customer_id: int):
    """Sync a Supabase customer record to QBO."""

    # --- 1. Fetch customer from Supabase ---
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"],
        sslmode="require",
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT qbo_customer_id, display_name, first_name, last_name,
               email, phone, street, city, state, zip,
               service_street, service_city, service_state, service_zip
        FROM public."Customers"
        WHERE id = %s
    """, (customer_id,))
    customer = cur.fetchone()
    cur.close()
    conn.close()

    if not customer:
        return {"success": False, "error": f"Customer {customer_id} not found"}

    qbo_id = customer.get("qbo_customer_id")
    if not qbo_id:
        return {"success": False, "error": f"Customer {customer_id} has no qbo_customer_id — skipping"}

    # --- 2. QBO Token Refresh ---
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)

    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")

    tokens = response.json()
    access_token = tokens["access_token"]
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)

    realm_id = resource["realm_id"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # --- 3. Fetch current QBO customer (need SyncToken) ---
    read_resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{qbo_id}?minorversion=73",
        headers=headers,
    )
    if not read_resp.ok:
        return {"success": False, "error": f"Failed to read QBO customer {qbo_id}: {read_resp.status_code}"}

    qbo_customer = read_resp.json()["Customer"]
    sync_token = qbo_customer["SyncToken"]

    # --- 4. Build update payload ---
    display_name = customer.get("display_name") or f"{customer.get('last_name', '')}, {customer.get('first_name', '')}".strip(", ")

    update_body = {
        "Id": qbo_id,
        "SyncToken": sync_token,
        "sparse": True,
        "DisplayName": display_name,
        "GivenName": customer.get("first_name") or None,
        "FamilyName": customer.get("last_name") or None,
    }

    email = customer.get("email")
    if email:
        update_body["PrimaryEmailAddr"] = {"Address": email}

    phone = customer.get("phone")
    if phone:
        update_body["PrimaryPhone"] = {"FreeFormNumber": phone}

    # Billing address
    bill_street = customer.get("street")
    if bill_street:
        update_body["BillAddr"] = {
            "Line1": bill_street,
            "City": customer.get("city") or None,
            "CountrySubDivisionCode": customer.get("state") or "GA",
            "PostalCode": customer.get("zip") or None,
        }

    # Service (shipping) address
    svc_street = customer.get("service_street")
    if svc_street:
        update_body["ShipAddr"] = {
            "Line1": svc_street,
            "City": customer.get("service_city") or None,
            "CountrySubDivisionCode": customer.get("service_state") or "GA",
            "PostalCode": customer.get("service_zip") or None,
        }

    # Clean nulls
    update_body = {k: v for k, v in update_body.items() if v is not None}

    # --- 5. Push update to QBO ---
    resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer?minorversion=73",
        headers=headers,
        json=update_body,
    )

    if not resp.ok:
        return {
            "success": False,
            "error": f"QBO update failed: {resp.status_code} - {resp.text[:500]}",
            "customer_id": customer_id,
            "qbo_customer_id": qbo_id,
        }

    final = resp.json()["Customer"]
    return {
        "success": True,
        "customer_id": customer_id,
        "qbo_customer_id": qbo_id,
        "display_name": final["DisplayName"],
    }
