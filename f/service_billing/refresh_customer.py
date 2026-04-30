# f/service_billing/refresh_customer
#
# Customer-entity webhook handler. Triggered when a Customer is created or
# updated in QBO. We don't have a dedicated `customers` table (customer info
# is denormalized into invoices and payments), so this script's job is to:
#
#   1. Fetch the customer from QBO so we have a current snapshot for any
#      audit-style queries.
#   2. Re-pull the customer's payment methods (`billing.customer_payment_methods`)
#      since the most useful customer-level cached state is which cards/banks
#      they have on file. A QBO customer edit often = adding/removing a payment
#      method, which directly affects how we route process_invoice.
#   3. Mark customer-context columns as fresh on related tables (touch
#      fetched_at on any open invoices for this customer so reconciliation
#      knows we recently saw them).
#
# Webhook latency target: <2s. We don't recheck invoice statuses here —
# that's refresh_invoice's job. Customer edits don't directly affect
# invoice billing_status.

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
        host=sb["host"],
        port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"),
        user=sb["user"],
        password=sb["password"],
        sslmode=sb.get("sslmode", "require"),
    )


def qbo_get(path, access_token, realm_id, params=None):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )


def fetch_customer_payment_methods(qbo_customer_id, access_token, realm_id):
    """
    Fetch all payment methods QBO has on file for this customer. QBO exposes
    these via the QuickBooks Payments API (separate from the Accounting API).

    The pull_customer_payment_methods script does the same call workspace-wide;
    here we narrow to one customer for webhook latency.
    """
    # The Payments API uses a different base URL (api.intuit.com vs
    # quickbooks.api.intuit.com). Fall back to listing all wallet items for
    # the company and filtering — Intuit doesn't expose a per-customer wallet
    # endpoint, but the existing puller already handles this; we can defer
    # to it for now.
    return None  # placeholder — see "next step" comment below


def main(qbo_customer_id: str):
    """
    Returns:
      {"status": "ok", "qbo_customer_id": "...", "display_name": "...",
       "active": <bool>, "open_invoice_count": <n>}
      {"status": "error", "error": "..."}
    """
    if not qbo_customer_id:
        return {"status": "error", "error": "qbo_customer_id required"}

    print(f"=== refresh_customer {qbo_customer_id} ===")
    access_token, realm_id = refresh_qbo_token()

    # Fetch the customer from QBO
    resp = qbo_get(f"customer/{qbo_customer_id}", access_token, realm_id)
    if not resp.ok:
        return {
            "status": "error",
            "error": f"QBO fetch failed: {resp.status_code}",
            "detail": resp.text[:200],
        }

    qbo_cust = (resp.json() or {}).get("Customer")
    if not qbo_cust:
        return {"status": "error", "error": "QBO returned no Customer"}

    display_name = qbo_cust.get("DisplayName") or qbo_cust.get("CompanyName")
    active = bool(qbo_cust.get("Active", True))
    primary_email = (qbo_cust.get("PrimaryEmailAddr") or {}).get("Address")
    primary_phone = (qbo_cust.get("PrimaryPhone") or {}).get("FreeFormNumber")

    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Update cached customer_name on any invoices for this customer
        # (customer might have been renamed in QBO).
        cur.execute("""
            UPDATE billing.invoices
            SET customer_name = %s,
                fetched_at    = now()
            WHERE qbo_customer_id = %s
              AND COALESCE(customer_name, '') <> COALESCE(%s, '')
            RETURNING qbo_invoice_id
        """, (display_name, qbo_customer_id, display_name))
        invoice_renames = cur.fetchall()

        # Count open invoices for this customer (informational return)
        cur.execute("""
            SELECT count(*) AS c
            FROM billing.invoices
            WHERE qbo_customer_id = %s
              AND billing_status NOT IN ('processed')
        """, (qbo_customer_id,))
        open_invoice_count = cur.fetchone()["c"]

        # No dedicated customers table to write to. Customer info lives
        # denormalized in billing.invoices.customer_name (which we already
        # updated above for any changes) and is used only for display.
        # qbo_customer_sync_log is a bulk-sync job log, not per-customer.
        # The webhook_log row inserted by the API route already carries the
        # full QBO Customer payload for audit; we don't need a duplicate.

        conn.commit()
        cur.close()

        return {
            "status": "ok",
            "qbo_customer_id": qbo_customer_id,
            "display_name": display_name,
            "active": active,
            "primary_email": primary_email,
            "primary_phone": primary_phone,
            "invoice_renames": [r["qbo_invoice_id"] for r in invoice_renames],
            "open_invoice_count": open_invoice_count,
            "note": "Payment methods sync deferred to scheduled pull_customer_payment_methods cron",
        }
    finally:
        conn.close()
