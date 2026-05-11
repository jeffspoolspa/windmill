# Pull customer payment methods (cards + ACH) from QBO Payments API v4
# into billing.customer_payment_methods.
#
# Only fetches for customers that have billable WOs (invoice_number IS NOT NULL).
# Uses the same QBO Payments v4 endpoints as the original
# service_billing_processing script:
#   - GET /quickbooks/v4/customers/{id}/cards
#   - GET /quickbooks/v4/customers/{id}/bank-accounts
#
# Schedule: every 4 hours.

import requests
import wmill
import psycopg2
import psycopg2.extras
import uuid
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
CACHE_TTL_MINUTES = 240  # 4 hours — don't re-fetch recently cached customers


def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]), timeout=30,
    )
    if not resp.ok:
        raise Exception(f"QBO token refresh failed: {resp.status_code} - {resp.text}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def fetch_methods_for_customer(customer_id: str, access_token: str) -> list[dict]:
    """Fetch cards + ACH for one customer from QBO Payments API v4."""
    methods = []
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Request-Id": str(uuid.uuid4()),
    }

    # Cards
    try:
        r = requests.get(
            f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/cards",
            headers=headers, timeout=20,
        )
        if r.ok:
            cards = r.json() if isinstance(r.json(), list) else []
            for c in cards:
                if c.get("status") == "ACTIVE":
                    methods.append({
                        # cpm_type_check requires 'credit_card' or 'ach'.
                        # The legacy 'card' value crashed every scheduled
                        # run since the constraint was tightened.
                        "type": "credit_card",
                        "qbo_payment_method_id": c.get("id"),
                        "card_brand": c.get("cardType"),
                        "last_four": (c.get("number") or "")[-4:],
                        # QBO returns `default: true/false` on each card
                        # (not nullable). Read it through; ACH does the
                        # same below. Without this, every card came in
                        # is_default=false, breaking PM-type resolution.
                        "is_default": bool(c.get("default")),
                        "raw": c,
                    })
    except Exception as e:
        print(f"  card error for {customer_id}: {e}")

    # ACH
    try:
        r = requests.get(
            f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/bank-accounts",
            headers={**headers, "Request-Id": str(uuid.uuid4())},
            timeout=20,
        )
        if r.ok:
            banks = r.json() if isinstance(r.json(), list) else []
            for b in banks:
                if b.get("verificationStatus") in ("VERIFIED", "NOT_VERIFIED"):
                    methods.append({
                        "type": "ach",
                        "qbo_payment_method_id": b.get("id"),
                        "card_brand": b.get("bankName"),
                        "last_four": (b.get("accountNumber") or "")[-4:],
                        "is_default": bool(b.get("default")),
                        "raw": b,
                    })
    except Exception as e:
        print(f"  bank error for {customer_id}: {e}")

    return methods


def main(force_refresh: bool = False):
    """Refresh customer payment methods for all billable customers.

    Args:
        force_refresh: If True, re-fetch all regardless of cache TTL.
    """
    print(f"=== pull_customer_payment_methods started (force={force_refresh}) ===")

    conn = get_db_conn()
    cur = conn.cursor()

    # Find unique QBO customer IDs for billable WOs
    if force_refresh:
        cur.execute("""
            SELECT DISTINCT i.qbo_customer_id
            FROM public.work_orders w
            JOIN billing.invoices i ON i.doc_number = w.invoice_number
            WHERE w.invoice_number IS NOT NULL AND i.qbo_customer_id IS NOT NULL
        """)
    else:
        # Skip customers we already fetched recently
        cur.execute("""
            SELECT DISTINCT i.qbo_customer_id
            FROM public.work_orders w
            JOIN billing.invoices i ON i.doc_number = w.invoice_number
            WHERE w.invoice_number IS NOT NULL AND i.qbo_customer_id IS NOT NULL
              AND i.qbo_customer_id NOT IN (
                SELECT DISTINCT qbo_customer_id FROM billing.customer_payment_methods
                WHERE fetched_at > now() - interval '%s minutes'
              )
        """ % CACHE_TTL_MINUTES)

    customer_ids = [r[0] for r in cur.fetchall()]
    cur.close()
    print(f"Found {len(customer_ids)} customers to fetch")

    if not customer_ids:
        conn.close()
        return {"status": "nothing_to_fetch", "customers": 0}

    access_token = refresh_qbo_token()
    now = datetime.now(timezone.utc)
    cur = conn.cursor()

    stats = {"customers": 0, "with_methods": 0, "total_methods": 0, "cards": 0, "ach": 0}

    stats["customer_errors"] = 0
    for i, cid in enumerate(customer_ids):
        try:
            methods = fetch_methods_for_customer(cid, access_token)
            stats["customers"] += 1

            # Deactivate old entries for this customer
            cur.execute(
                "UPDATE billing.customer_payment_methods SET is_active = false WHERE qbo_customer_id = %s",
                (cid,),
            )

            if methods:
                stats["with_methods"] += 1
                for m in methods:
                    cur.execute("""
                        INSERT INTO billing.customer_payment_methods
                            (qbo_customer_id, qbo_payment_method_id, type, card_brand,
                             last_four, is_default, is_active, raw, fetched_at)
                        VALUES (%s, %s, %s, %s, %s, %s, true, %s::jsonb, %s)
                        ON CONFLICT (qbo_customer_id, qbo_payment_method_id) DO UPDATE SET
                            type = EXCLUDED.type, card_brand = EXCLUDED.card_brand,
                            last_four = EXCLUDED.last_four, is_default = EXCLUDED.is_default,
                            is_active = true, raw = EXCLUDED.raw, fetched_at = EXCLUDED.fetched_at
                    """, (
                        cid, m["qbo_payment_method_id"], m["type"],
                        m["card_brand"], m["last_four"], m["is_default"],
                        psycopg2.extras.Json(m.get("raw", {})), now,
                    ))
                    stats["total_methods"] += 1
                    if m["type"] == "credit_card":
                        stats["cards"] += 1
                    else:
                        stats["ach"] += 1

            conn.commit()
        except Exception as e:
            # Per-customer isolation. Without this, one bad row (a new
            # cardType QBO returns that we don't model, an unexpected
            # constraint, etc.) crashes the entire sweep and every
            # downstream customer goes unfetched — exactly how RIGBY's
            # card got missed for days. Roll back the failed transaction,
            # log, and keep going.
            stats["customer_errors"] += 1
            print(f"  ERROR on customer {cid}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            # Re-acquire cursor — psycopg2 cursors can become unusable
            # after a transaction abort.
            cur.close()
            cur = conn.cursor()

        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(customer_ids)} customers (errors so far: {stats['customer_errors']})")

    cur.close()
    conn.close()

    print(f"=== done: {stats} ===")
    return {"status": "success", **stats}
