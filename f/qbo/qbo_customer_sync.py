# QBO Customer Sync (Production)
# Runs daily at 5am ET - syncs all customers, soft-deletes removed ones
# Billing address (BillAddr) goes to Customers table
# Service address (ShipAddr) goes to service_locations table (is_primary=true)
# Handles QBO address quirk: Line1=name, Line2=street when both exist

import requests
import psycopg2
import psycopg2.extras
import wmill
import time
import re
from datetime import datetime, timezone


QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30


def get_pg_connection():
    supabase = wmill.get_resource(SUPABASE_RESOURCE)
    conn = psycopg2.connect(
        host=supabase["host"], port=supabase["port"],
        dbname=supabase["dbname"], user=supabase["user"],
        password=supabase["password"], connect_timeout=10
    )
    return conn


def create_sync_log(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO qbo_customer_sync_log (status) VALUES ('running') RETURNING id")
        log_id = cur.fetchone()[0]
        conn.commit()
    print(f"✓ Created sync log (ID: {log_id})")
    return log_id


def update_sync_log(conn, log_id, status, records_synced=None, records_deleted=None,
                    active_count=None, inactive_count=None, error_message=None, error_step=None):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE qbo_customer_sync_log SET completed_at = NOW(), status = %s,
                records_synced = %s, records_deleted = %s, active_count = %s,
                inactive_count = %s, error_message = %s, error_step = %s
            WHERE id = %s
        """, (status, records_synced, records_deleted, active_count,
              inactive_count, error_message, error_step, log_id))
        conn.commit()


def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    try:
        response = requests.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
            auth=(resource["client_id"], resource["client_secret"]), timeout=REQUEST_TIMEOUT
        )
    except requests.exceptions.Timeout:
        raise Exception("QBO token refresh timed out")
    except requests.exceptions.ConnectionError as e:
        raise Exception(f"QBO token refresh connection failed: {e}")

    if response.status_code == 401:
        raise Exception("QBO refresh token burned — manual reauth required")
    if not response.ok:
        raise Exception(f"QBO token refresh failed: HTTP {response.status_code}")

    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    print(f"✓ QBO token refreshed (realm: {resource['realm_id']})")
    return tokens["access_token"], resource["realm_id"]


def qbo_query(access_token, realm_id, query):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                params={"query": query}, timeout=REQUEST_TIMEOUT
            )
            if response.status_code == 401:
                raise Exception("QBO API 401 - token expired mid-sync")
            if response.status_code == 429:
                time.sleep(2 ** (attempt + 2))
                continue
            if not response.ok:
                raise Exception(f"QBO API: HTTP {response.status_code}")
            return response.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = str(e)
        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)
    raise Exception(f"QBO query failed after {MAX_RETRIES} attempts: {last_error}")


def fetch_all_customers(access_token, realm_id):
    all_customers = []
    start = 1
    page = 1
    while True:
        print(f"  Fetching page {page} (records {start}-{start + 999})...")
        result = qbo_query(access_token, realm_id,
                           f"SELECT * FROM Customer STARTPOSITION {start} MAXRESULTS 1000")
        customers = result.get('QueryResponse', {}).get('Customer', [])
        if not customers:
            break
        all_customers.extend(customers)
        print(f"  Page {page}: {len(customers)} customers (total: {len(all_customers)})")
        if len(customers) < 1000:
            break
        start += 1000
        page += 1
    print(f"✓ Fetched {len(all_customers)} customers from QBO")
    return all_customers


def extract_street(addr):
    """Extract the actual street from a QBO address block.
    QBO often puts the customer name in Line1 and the street in Line2.
    If Line2 exists and looks like an address, use it. Otherwise use Line1.
    """
    if not addr:
        return None
    line1 = (addr.get("Line1") or "").strip()
    line2 = (addr.get("Line2") or "").strip()

    if line2:
        if re.match(r'^\d', line2):
            return line2
        if re.match(r'^\d', line1):
            return line1
        return line2
    return line1 if line1 else None


def sync_customers_to_supabase(conn, customers):
    stats = {"synced": 0, "soft_deleted": 0, "active": 0, "inactive": 0, "service_locations": 0}
    if not customers:
        return stats

    stats["active"] = sum(1 for c in customers if c.get('Active', True))
    stats["inactive"] = len(customers) - stats["active"]
    print(f"Processing {len(customers)} customers ({stats['active']} active, {stats['inactive']} inactive)...")

    all_qbo_ids = [c.get('Id') for c in customers]

    with conn.cursor() as cur:
        print("  Upserting customers...")
        cur.executemany("""
            INSERT INTO "Customers" (
                qbo_customer_id, display_name, company, first_name, last_name,
                email, phone, customer_type,
                street, city, state, zip,
                is_active, balance, notes, qbo_last_updated, deleted_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NULL)
            ON CONFLICT (qbo_customer_id) DO UPDATE SET
                display_name=EXCLUDED.display_name, company=EXCLUDED.company,
                first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name,
                email=EXCLUDED.email, phone=EXCLUDED.phone,
                customer_type=EXCLUDED.customer_type,
                street=EXCLUDED.street, city=EXCLUDED.city,
                state=EXCLUDED.state, zip=EXCLUDED.zip,
                is_active=EXCLUDED.is_active, balance=EXCLUDED.balance,
                notes=EXCLUDED.notes, qbo_last_updated=EXCLUDED.qbo_last_updated,
                deleted_at=NULL
        """, [
            (
                c.get('Id'),
                c.get('DisplayName'),
                c.get('CompanyName'),
                c.get('GivenName'),
                c.get('FamilyName'),
                c.get('PrimaryEmailAddr', {}).get('Address') if c.get('PrimaryEmailAddr') else None,
                c.get('PrimaryPhone', {}).get('FreeFormNumber') if c.get('PrimaryPhone') else None,
                c.get('CustomerTypeRef', {}).get('name') if c.get('CustomerTypeRef') else None,
                extract_street(c.get('BillAddr')),
                c.get('BillAddr', {}).get('City') if c.get('BillAddr') else None,
                c.get('BillAddr', {}).get('CountrySubDivisionCode') if c.get('BillAddr') else None,
                c.get('BillAddr', {}).get('PostalCode') if c.get('BillAddr') else None,
                c.get('Active', True),
                c.get('Balance'),
                c.get('Notes'),
                c.get('MetaData', {}).get('LastUpdatedTime'),
            )
            for c in customers
        ])
        stats["synced"] = len(customers)
        print(f"  ✓ Upserted {stats['synced']} customers")

        # Upsert service locations from ShipAddr into service_locations table.
        # The uniqueness target is a PARTIAL unique index (account_id WHERE is_primary).
        # Partial indexes can't back a named table constraint, so we infer the
        # conflict target by (account_id) WHERE is_primary to match the partial index.
        print("  Upserting service locations from ShipAddr...")
        customers_with_ship = [c for c in customers if extract_street(c.get('ShipAddr'))]
        if customers_with_ship:
            qbo_ids_with_ship = [c.get('Id') for c in customers_with_ship]
            cur.execute(
                'SELECT id, qbo_customer_id FROM "Customers" WHERE qbo_customer_id = ANY(%s)',
                (qbo_ids_with_ship,)
            )
            id_map = {row[1]: row[0] for row in cur.fetchall()}

            service_rows = []
            for c in customers_with_ship:
                ship = c.get('ShipAddr', {})
                street = extract_street(ship)
                if not street:
                    continue
                internal_id = id_map.get(c.get('Id'))
                if not internal_id:
                    continue
                service_rows.append((
                    internal_id,
                    street,
                    ship.get('City'),
                    ship.get('CountrySubDivisionCode'),
                    ship.get('PostalCode'),
                    True,  # is_primary
                    True,  # is_active
                ))

            if service_rows:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO service_locations (account_id, street, city, state, zip, is_primary, is_active)
                    VALUES %s
                    ON CONFLICT (account_id) WHERE is_primary DO UPDATE SET
                        street = EXCLUDED.street,
                        city = EXCLUDED.city,
                        state = EXCLUDED.state,
                        zip = EXCLUDED.zip,
                        updated_at = NOW()
                """, service_rows)
                stats["service_locations"] = len(service_rows)
                print(f"  ✓ Upserted {stats['service_locations']} service locations")
        else:
            print("  (no ShipAddr records to sync)")

        print("  Checking for removed customers...")
        cur.execute("""
            UPDATE "Customers" SET deleted_at = NOW(), is_active = false
            WHERE qbo_customer_id IS NOT NULL AND deleted_at IS NULL
            AND qbo_customer_id != ALL(%s)
        """, (all_qbo_ids,))
        stats["soft_deleted"] = cur.rowcount
        if stats["soft_deleted"] > 0:
            print(f"  ✓ Soft-deleted {stats['soft_deleted']} customers")
        conn.commit()

    return stats


def main():
    print("=" * 60)
    print("QBO CUSTOMER SYNC STARTED")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    conn = None
    log_id = None
    current_step = "initializing"

    try:
        current_step = "connecting to database"
        conn = get_pg_connection()
        current_step = "creating sync log"
        log_id = create_sync_log(conn)
        current_step = "refreshing QBO token"
        access_token, realm_id = refresh_qbo_token()
        current_step = "fetching customers from QBO"
        customers = fetch_all_customers(access_token, realm_id)
        current_step = "syncing to Supabase"
        stats = sync_customers_to_supabase(conn, customers)
        current_step = "finalizing"
        update_sync_log(conn, log_id, "success",
                        records_synced=stats["synced"], records_deleted=stats["soft_deleted"],
                        active_count=stats["active"], inactive_count=stats["inactive"])

        print(f"\n✓ DONE: {stats['synced']} synced, {stats['service_locations']} service locations, {stats['soft_deleted']} soft-deleted")
        return {"status": "success", "total_synced": stats["synced"],
                "active": stats["active"], "inactive": stats["inactive"],
                "service_locations_synced": stats["service_locations"],
                "soft_deleted": stats["soft_deleted"], "log_id": log_id}

    except Exception as e:
        error_msg = str(e)
        print(f"\n✗ FAILED at {current_step}: {error_msg}")
        if conn and log_id:
            # pg transaction may be aborted by the error; roll back so the
            # best-effort log write can commit on the same connection.
            try:
                conn.rollback()
                update_sync_log(conn, log_id, "failed", error_message=error_msg, error_step=current_step)
            except Exception as log_err:
                print(f"  (also failed to write sync log: {log_err})")
        # Re-raise so Windmill marks the job as failed. Previously the caught
        # exception returned a dict, which Windmill treats as success — hiding
        # broken runs.
        raise
    finally:
        if conn:
            conn.close()
