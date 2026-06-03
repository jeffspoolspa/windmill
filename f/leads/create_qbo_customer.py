
import requests
import wmill
import psycopg2
import psycopg2.extras

# Create a QBO customer for a converted lead and link it back to Supabase (Gen-2).
#
# In the canonical "Gen-2" leads model the Customers row already exists at intake
# (check_or_create_customer creates/dedups it), so this script does NOT create a
# Supabase customer. It: looks up the lead's account, creates the QBO customer if
# the account isn't already linked, then stamps public."Customers".qbo_customer_id
# via the update_lead_qbo_customer RPC (which also logs to maintenance.lead_activities).
#
# Replaces the dead Gen-1 version that wrote the nonexistent maintenance.leads /
# maintenance.lead_activities. See docs/adrs/004-leads-canonical-model.md and
# docs/flows/lead-intake-to-conversion.md.


def main(
    lead_id: str,
    # Kept for backward-compat with the legacy caller's payload; values fetched
    # from Supabase take precedence, these are fallbacks only.
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
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"],
        sslmode="require",
    )
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # --- 1. Resolve the lead's account (the Customer already exists in Gen-2) ---
    cur.execute("""
        SELECT l.account_id,
               c.qbo_customer_id, c.display_name, c.first_name, c.last_name,
               c.email, c.phone, c.street, c.city, c.state, c.zip
        FROM public.leads l
        JOIN public."Customers" c ON c.id = l.account_id
        WHERE l.id = %s::uuid
    """, (lead_id,))
    row = cur.fetchone()

    if not row:
        cur.close(); conn.close()
        return {"success": False, "error": f"Lead {lead_id} not found or has no linked account"}

    account_id = row["account_id"]

    # Idempotent: if already linked, don't create a duplicate QBO customer.
    if row["qbo_customer_id"]:
        cur.close(); conn.close()
        return {
            "success": True,
            "lead_id": lead_id,
            "qbo_customer_id": row["qbo_customer_id"],
            "supabase_customer_id": account_id,
            "already_linked": True,
        }

    first_name = row["first_name"] or first_name or ""
    last_name = row["last_name"] or last_name or ""
    email = row["email"] or email or ""
    phone = row["phone"] or phone or ""
    street = row["street"] or address_street or ""
    city = row["city"] or address_city or ""
    state = row["state"] or address_state or "GA"
    zip_code = row["zip"] or address_zip or ""

    # --- 2. QBO token refresh (rotating refresh token — MUST save the new one) ---
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)

    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not response.ok:
        cur.close(); conn.close()
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")

    tokens = response.json()
    access_token = tokens["access_token"]
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)  # CRITICAL: persist rotated token

    realm_id = resource["realm_id"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # --- 3. Build display name + create the QBO customer ---
    display_name = (row["display_name"] or "").strip()
    if not display_name:
        display_name = f"{last_name}, {first_name}".strip(", ").strip() or email or "Unknown Lead"

    customer_body = {
        "DisplayName": display_name,
        "GivenName": first_name or None,
        "FamilyName": last_name or None,
        "Notes": f"Created on lead conversion. Lead ID: {lead_id}",
    }
    if email:
        customer_body["PrimaryEmailAddr"] = {"Address": email}
    if phone:
        customer_body["PrimaryPhone"] = {"FreeFormNumber": phone}
    if street:
        customer_body["BillAddr"] = {
            "Line1": street,
            "City": city or None,
            "CountrySubDivisionCode": state or "GA",
            "PostalCode": zip_code or None,
        }
    customer_body = {k: v for k, v in customer_body.items() if v is not None}

    resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer?minorversion=73",
        headers=headers,
        json=customer_body,
    )

    # QBO rejects duplicate DisplayName — retry with the street appended.
    if resp.status_code == 400 and "already being used" in resp.text.lower():
        street_label = street.strip() if street else "New"
        customer_body["DisplayName"] = f"{display_name} ({street_label})"
        display_name = customer_body["DisplayName"]
        resp = requests.post(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer?minorversion=73",
            headers=headers,
            json=customer_body,
        )

    if not resp.ok:
        cur.close(); conn.close()
        return {
            "success": False,
            "error": f"QBO customer creation failed: {resp.status_code} - {resp.text[:500]}",
            "lead_id": lead_id,
        }

    qbo_customer = resp.json()["Customer"]
    qbo_customer_id = qbo_customer["Id"]
    final_display_name = qbo_customer["DisplayName"]

    # --- 4. Stamp Customers.qbo_customer_id + log activity (Gen-2 RPC) ---
    cur.execute(
        "SELECT public.update_lead_qbo_customer(%s::uuid, %s, %s)",
        (lead_id, qbo_customer_id, account_id),
    )

    cur.close()
    conn.close()

    return {
        "success": True,
        "lead_id": lead_id,
        "qbo_customer_id": qbo_customer_id,
        "supabase_customer_id": account_id,
        "display_name": final_display_name,
    }
