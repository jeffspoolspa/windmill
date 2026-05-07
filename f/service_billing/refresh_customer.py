# f/service_billing/refresh_customer
#
# Single-customer QBO -> Supabase refresh.
#
# Callers:
#   - QBO webhook handler:   main(qbo_customer_id)
#                            — fetches the customer from QBO and refreshes
#   - cdc_reconciler:        main(qbo_customer_id, qbo_body=<cdc_entity>)
#                            — passes the body it already has from CDC,
#                              skipping the QBO GET. Single source of truth
#                              for the upsert + side effects.
#
# Concurrency: the upsert uses an OCC guard on qbo_last_updated. Two
# concurrent callers writing the same customer never clobber each other.
#
# Side effects: display_name renames are propagated to billing.invoices.
# customer_name. Gated on did_write so the race loser doesn't overwrite
# invoice cache rows with its stale display_name.

import json
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _dumps(obj):
    return json.dumps(obj, default=_json_default)


def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
        timeout=30,
    )
    if not resp.ok:
        raise Exception(f"QBO token refresh failed: {resp.status_code} - {resp.text}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"], resource["realm_id"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def qbo_get(path, access_token, realm_id):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )


def parse_qbo_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def upsert_customer(conn, qbo_cust):
    """Upsert with OCC guard. Returns (qbo_customer_id, display_name, did_write, row).

    OCC: only updates when EXCLUDED.qbo_last_updated is strictly newer than
    the existing row's. New inserts (no conflict) always land. Race-loser's
    UPDATE matches zero rows; downstream side effects skip via did_write.
    """
    qbo_customer_id = qbo_cust.get("Id")
    display_name    = qbo_cust.get("DisplayName")
    given_name      = qbo_cust.get("GivenName")
    family_name     = qbo_cust.get("FamilyName")
    company_name    = qbo_cust.get("CompanyName")
    is_active       = bool(qbo_cust.get("Active", True))
    balance         = float(qbo_cust.get("Balance") or 0)
    primary_email   = (qbo_cust.get("PrimaryEmailAddr") or {}).get("Address")
    primary_phone   = (qbo_cust.get("PrimaryPhone") or {}).get("FreeFormNumber")

    bill_addr = qbo_cust.get("BillAddr") or {}
    street_parts = [bill_addr.get("Line1"), bill_addr.get("Line2"), bill_addr.get("Line3")]
    street = ", ".join(p for p in street_parts if p)
    city = bill_addr.get("City")
    state = bill_addr.get("CountrySubDivisionCode")
    zip_code = bill_addr.get("PostalCode")
    latitude = bill_addr.get("Lat")
    longitude = bill_addr.get("Long")
    try:
        latitude = float(latitude) if latitude is not None else None
        longitude = float(longitude) if longitude is not None else None
    except (ValueError, TypeError):
        latitude = longitude = None

    qbo_last_updated = parse_qbo_timestamp(
        (qbo_cust.get("MetaData") or {}).get("LastUpdatedTime")
    )

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        INSERT INTO public."Customers"
          (qbo_customer_id, display_name, first_name, last_name, company,
           street, city, state, zip, phone, email,
           is_active, balance, latitude, longitude,
           qbo_last_updated, imported_at, deleted_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, now(), NULL)
        ON CONFLICT (qbo_customer_id) DO UPDATE SET
          display_name     = EXCLUDED.display_name,
          first_name       = EXCLUDED.first_name,
          last_name        = EXCLUDED.last_name,
          company          = EXCLUDED.company,
          street           = EXCLUDED.street,
          city             = EXCLUDED.city,
          state            = EXCLUDED.state,
          zip              = EXCLUDED.zip,
          phone            = EXCLUDED.phone,
          email            = EXCLUDED.email,
          is_active        = EXCLUDED.is_active,
          balance          = EXCLUDED.balance,
          latitude         = COALESCE(EXCLUDED.latitude, public."Customers".latitude),
          longitude        = COALESCE(EXCLUDED.longitude, public."Customers".longitude),
          qbo_last_updated = EXCLUDED.qbo_last_updated,
          deleted_at       = NULL
        WHERE public."Customers".qbo_last_updated IS NULL
           OR EXCLUDED.qbo_last_updated IS NULL
           OR public."Customers".qbo_last_updated < EXCLUDED.qbo_last_updated
        RETURNING id, qbo_customer_id, display_name, is_active
    ''', (
        qbo_customer_id, display_name, given_name, family_name, company_name,
        street or None, city, state, zip_code, primary_phone, primary_email,
        is_active, balance, latitude, longitude, qbo_last_updated,
    ))
    row = cur.fetchone()
    cur.close()
    return qbo_customer_id, display_name, (row is not None), (dict(row) if row else None)


def main(qbo_customer_id: str, qbo_body: dict | None = None):
    """
    Args:
      qbo_customer_id: Required. QBO Id of the customer.
      qbo_body:        Optional. Pre-fetched QBO Customer body (e.g. from CDC).
                       When provided, skips the QBO GET.
    """
    if not qbo_customer_id:
        return {"status": "error", "error": "qbo_customer_id required"}

    print(f"=== refresh_customer {qbo_customer_id} (body_provided={qbo_body is not None}) ===")

    qbo_cust = qbo_body
    if qbo_cust is None:
        access_token, realm_id = refresh_qbo_token()
        resp = qbo_get(f"customer/{qbo_customer_id}", access_token, realm_id)

        if resp.status_code == 404:
            conn = get_db_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    'UPDATE public."Customers" SET deleted_at = now() WHERE qbo_customer_id = %s AND deleted_at IS NULL',
                    (qbo_customer_id,),
                )
                affected = cur.rowcount
                conn.commit()
                cur.close()
                return {"status": "deleted", "qbo_customer_id": qbo_customer_id,
                        "rows_marked_deleted": affected}
            finally:
                conn.close()

        if not resp.ok:
            return {"status": "error",
                    "error": f"QBO fetch failed: {resp.status_code}",
                    "detail": resp.text[:200]}

        qbo_cust = (resp.json() or {}).get("Customer")
        if not qbo_cust:
            return {"status": "error", "error": "QBO returned no Customer"}

    conn = get_db_conn()
    try:
        qbo_customer_id, display_name, did_write, upserted = upsert_customer(conn, qbo_cust)
        conn.commit()

        # display_name propagation: only fire if our upsert actually wrote.
        # If OCC blocked us (someone newer landed first), the display_name we
        # have is stale relative to current cache state and would clobber
        # invoice rows with old data.
        invoice_renames = []
        if did_write:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute('''
                UPDATE billing.invoices
                SET customer_name = %s,
                    fetched_at    = now()
                WHERE qbo_customer_id = %s
                  AND COALESCE(customer_name, '') <> COALESCE(%s, '')
                RETURNING qbo_invoice_id
            ''', (display_name, qbo_customer_id, display_name))
            invoice_renames = [r["qbo_invoice_id"] for r in cur.fetchall()]
            conn.commit()
            cur.close()
        else:
            print(f"  upsert no-op (OCC blocked — newer state already in cache)")

        return {
            "status":           "ok",
            "qbo_customer_id":  qbo_customer_id,
            "id":               upserted["id"] if upserted else None,
            "display_name":     display_name,
            "is_active":        bool(qbo_cust.get("Active", True)),
            "balance":          float(qbo_cust.get("Balance") or 0),
            "did_write":        did_write,
            "invoice_renames":  invoice_renames,
        }
    finally:
        conn.close()
