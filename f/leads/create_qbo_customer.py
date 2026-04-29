
import requests
import wmill
import psycopg2
import psycopg2.extras

def main(
    lead_id: str,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    address_street: str = "",
    address_city: str = "",
    address_state: str = "GA",
    address_zip: str = "",
    source: str = "website",
):
    """Create a QBO customer for a new lead and link it back to Supabase."""

    # --- 1. QBO Token Refresh ---
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

    # --- 2. Build display name ---
    display_name = f"{last_name}, {first_name}".strip(", ").strip()
    if not display_name:
        display_name = email or "Unknown Lead"

    # --- 3. Create QBO Customer ---
    customer_body = {
        "DisplayName": display_name,
        "GivenName": first_name or None,
        "FamilyName": last_name or None,
        "Notes": f"Created via website quote form. Lead ID: {lead_id}",
    }
    if email:
        customer_body["PrimaryEmailAddr"] = {"Address": email}
    if phone:
        customer_body["PrimaryPhone"] = {"FreeFormNumber": phone}
    if address_street:
        customer_body["BillAddr"] = {
            "Line1": address_street,
            "City": address_city or None,
            "CountrySubDivisionCode": address_state or "GA",
            "PostalCode": address_zip or None,
        }

    customer_body = {k: v for k, v in customer_body.items() if v is not None}

    resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer?minorversion=73",
        headers=headers,
        json=customer_body,
    )

    # If QBO rejects due to duplicate name, retry with street address appended
    if resp.status_code == 400 and "already being used" in resp.text.lower():
        street_label = address_street.strip() if address_street else "New"
        customer_body["DisplayName"] = f"{display_name} ({street_label})"
        display_name = customer_body["DisplayName"]
        resp = requests.post(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer?minorversion=73",
            headers=headers,
            json=customer_body,
        )

    if not resp.ok:
        return {
            "success": False,
            "error": f"QBO customer creation failed: {resp.status_code} - {resp.text[:500]}",
            "lead_id": lead_id,
        }

    qbo_customer = resp.json()["Customer"]
    qbo_customer_id = qbo_customer["Id"]
    final_display_name = qbo_customer["DisplayName"]

    # --- 4. Upsert into public.Customers + update lead ---
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"],
        sslmode="require",
    )
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        INSERT INTO public."Customers" (
            qbo_customer_id, display_name, first_name, last_name,
            email, phone, street, city, state, zip,
            is_active, is_maintenance
        ) VALUES (
            %(qbo_id)s, %(display)s, %(first)s, %(last)s,
            %(email)s, %(phone)s, %(street)s, %(city)s, %(state)s, %(zip)s,
            true, true
        )
        ON CONFLICT (qbo_customer_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            email = COALESCE(EXCLUDED.email, public."Customers".email),
            phone = COALESCE(EXCLUDED.phone, public."Customers".phone),
            is_active = true,
            is_maintenance = true
        RETURNING id
    """, {
        "qbo_id": qbo_customer_id,
        "display": final_display_name,
        "first": first_name or None,
        "last": last_name or None,
        "email": email or None,
        "phone": phone or None,
        "street": address_street or None,
        "city": address_city or None,
        "state": address_state or "GA",
        "zip": address_zip or None,
    })
    row = cur.fetchone()
    supabase_customer_id = row["id"] if row else None

    cur.execute("""
        UPDATE maintenance.leads
        SET qbo_customer_id = %s,
            customer_id = %s,
            matched_customer_id = COALESCE(matched_customer_id, %s),
            updated_at = now()
        WHERE id = %s::uuid
    """, (qbo_customer_id, supabase_customer_id, supabase_customer_id, lead_id))

    cur.execute("""
        INSERT INTO maintenance.lead_activities (lead_id, activity_type, description, metadata, created_by)
        VALUES (%s::uuid, 'system', 'QBO customer created', %s::jsonb, 'windmill')
    """, (lead_id, psycopg2.extras.Json({
        "qbo_customer_id": qbo_customer_id,
        "display_name": final_display_name,
        "supabase_customer_id": supabase_customer_id,
    })))

    cur.close()
    conn.close()

    return {
        "success": True,
        "lead_id": lead_id,
        "qbo_customer_id": qbo_customer_id,
        "supabase_customer_id": supabase_customer_id,
        "display_name": final_display_name,
    }
