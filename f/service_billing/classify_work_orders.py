# Classify work orders that have invoice data cached and transition them
# to ready_to_process.
#
# For each WO in needs_classification:
#   1. Look up cached invoice → get qbo_customer_id
#   2. Look up / fetch customer payment methods (cards then ACH) via QBO Payments API v4
#   3. Compute payment_method:
#        - any of work_description / technician_instructions /
#          corrective_action contains *bill* → invoice  (manual override)
#        - customer has active card or ACH → on_file
#        - otherwise → invoice
#   4. Compute service_category + qbo_class from work_orders.type
#   5. UPDATE work_orders SET billing_status='ready_to_process', etc.
#
# Customer payment methods are cached in billing.customer_payment_methods
# (1 hour TTL by default) so repeated WOs for the same customer hit the
# cache instead of QBO.

import requests
import wmill
import psycopg2
import psycopg2.extras
import uuid
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
PAYMENT_METHOD_TTL_MINUTES = 60

# Map work_orders.type → (service_category, qbo_class)
# These are best-guess defaults. Override per customer/WO via classification_rules later.
TYPE_MAPPING: dict[str, tuple[str, str | None]] = {
    "GENERAL SERVICE": ("service", "Service"),
    "MAINTENANCE": ("maintenance", "Maintenance"),
    "DIAGNOSIS": ("service", "Service"),
    "DELIVERY": ("delivery", "Delivery"),
    "POOL INSPECTION": ("service", "Service"),
    "GO BACK": ("service", "Service"),
    "WARRANTY": ("warranty", "Warranty"),
    "ESTIMATE": ("estimate", "Estimate"),
    "ESTIMATE PREP": ("estimate", "Estimate"),
    "GREEN POOL ESTIMATE": ("service", "Service"),
    "LINER": ("install", "Install"),
    "COLD WATER DIVE": ("service", "Service"),
    "QUALITY CONTROL": ("internal", None),
    "HELPER TICKET": ("internal", None),
    "IN-HOUSE WORK": ("internal", None),
    "VEHICLE REPAIRS": ("internal", None),
    "INVENTORY": ("internal", None),
    "INTERNAL": ("internal", None),
    "HOLD": ("internal", None),
    "POOL SCHOOL": ("service", "Service"),
    "PHONE CALL ONLY": ("internal", None),
    "FIRST VISIT": ("service", "Service"),
    "CANCELLED": ("cancelled", None),
}


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
    return tokens["access_token"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def fetch_qbo_payment_methods(customer_id: str, access_token: str) -> list[dict]:
    """Fetch all active cards and ACH for a customer via QBO Payments API v4.

    Returns a list of {type, qbo_payment_method_id, card_brand, last_four, is_default} dicts.
    Empty list if customer has nothing on file or any error.
    """
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
                        "type": "card",
                        "qbo_payment_method_id": c.get("id"),
                        "card_brand": c.get("cardType"),
                        "last_four": (c.get("number") or "")[-4:],
                        "is_default": False,
                        "raw": c,
                    })
    except Exception as e:
        print(f"  card fetch error for {customer_id}: {e}")

    # Bank accounts (ACH)
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
        print(f"  bank fetch error for {customer_id}: {e}")

    return methods


def get_or_fetch_payment_methods(
    conn, customer_id: str, access_token: str, force_refresh: bool
) -> list[dict]:
    """Return active payment methods for a customer, hitting QBO only if cache is stale."""
    if not customer_id:
        return []

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if not force_refresh:
        cur.execute(
            """
            SELECT type, qbo_payment_method_id, card_brand, last_four, is_default, fetched_at
            FROM billing.customer_payment_methods
            WHERE qbo_customer_id = %s
              AND is_active = true
              AND fetched_at > (now() - (%s || ' minutes')::interval)
            """,
            (customer_id, str(PAYMENT_METHOD_TTL_MINUTES)),
        )
        cached = cur.fetchall()
        if cached:
            cur.close()
            return [dict(r) for r in cached]

    # Cache miss or force refresh — fetch from QBO
    methods = fetch_qbo_payment_methods(customer_id, access_token)

    # Mark any old rows for this customer as inactive (so a removed card disappears)
    cur.execute(
        "UPDATE billing.customer_payment_methods SET is_active = false WHERE qbo_customer_id = %s",
        (customer_id,),
    )

    now = datetime.now(timezone.utc)
    for m in methods:
        cur.execute(
            """
            INSERT INTO billing.customer_payment_methods
                (qbo_customer_id, qbo_payment_method_id, type, card_brand,
                 last_four, is_default, is_active, raw, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, true, %s::jsonb, %s)
            ON CONFLICT (qbo_customer_id, qbo_payment_method_id) DO UPDATE SET
                type = EXCLUDED.type,
                card_brand = EXCLUDED.card_brand,
                last_four = EXCLUDED.last_four,
                is_default = EXCLUDED.is_default,
                is_active = true,
                raw = EXCLUDED.raw,
                fetched_at = EXCLUDED.fetched_at
            """,
            (
                customer_id,
                m.get("qbo_payment_method_id"),
                m.get("type"),
                m.get("card_brand"),
                m.get("last_four"),
                m.get("is_default"),
                psycopg2.extras.Json(m.get("raw", {})),
                now,
            ),
        )
    conn.commit()
    cur.close()
    return methods


def classify_one(
    conn, wo: dict, access_token: str, force_refresh_payment: bool
) -> dict:
    """Classify a single work order. Returns the update applied."""
    wo_number = wo["wo_number"]
    wo_type = (wo.get("type") or "").upper()
    description = (wo.get("work_description") or "")
    customer_id = wo.get("qbo_customer_id_from_invoice")

    # 1. Service category + QBO class from type
    service_category, qbo_class = TYPE_MAPPING.get(wo_type, ("service", wo_type.title() if wo_type else None))

    # 2. Payment method decision
    # Check all three free-text fields, not just work_description. Office
    # staff write *bill* into whichever ION text field they're in at the
    # moment — historically the override was missed when *bill* lived in
    # tech instructions or corrective action (e.g., CHESSER 5007168,
    # OLSON 5000640 both auto-charged on 2026-05-21 because of this).
    bill_text = " ".join(filter(None, [
        wo.get("work_description"),
        wo.get("technician_instructions"),
        wo.get("corrective_action"),
    ])).lower()
    has_bill_override = "*bill*" in bill_text
    payment_methods = []
    if not has_bill_override and customer_id:
        payment_methods = get_or_fetch_payment_methods(
            conn, customer_id, access_token, force_refresh_payment
        )

    if has_bill_override:
        payment_method = "invoice"
        reason = "*bill* override"
    elif any(m for m in payment_methods if m.get("type") in ("card", "ach")):
        payment_method = "on_file"
        reason = f"{len(payment_methods)} method(s) on file"
    else:
        payment_method = "invoice"
        reason = "no payment method on file"

    # 3. Update work_orders
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE public.work_orders
        SET service_category = %s,
            qbo_class = %s,
            payment_method = %s,
            billing_status = 'ready_to_process',
            billing_status_set_at = now(),
            last_classified_at = now()
        WHERE wo_number = %s
        """,
        (service_category, qbo_class, payment_method, wo_number),
    )
    conn.commit()
    cur.close()

    return {
        "wo_number": wo_number,
        "type": wo_type,
        "service_category": service_category,
        "qbo_class": qbo_class,
        "payment_method": payment_method,
        "reason": reason,
        "customer_id": customer_id,
    }


def main(
    force_refresh_payment_methods: bool = False,
    limit: int = None,
) -> dict:
    """Classify work_orders in needs_classification → ready_to_process.

    Args:
        force_refresh_payment_methods: Re-fetch QBO wallet ignoring cache.
        limit: Cap rows for testing.
    """
    print(f"=== classify_work_orders started ===")
    print(f"force_refresh_payment_methods={force_refresh_payment_methods} limit={limit}")

    conn = get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Find WOs to classify: needs_classification AND we have invoice cached (so we know customer_id)
    sql = """
        SELECT
            w.wo_number,
            w.type,
            w.work_description,
            w.technician_instructions,
            w.corrective_action,
            w.invoice_number,
            i.qbo_customer_id AS qbo_customer_id_from_invoice,
            i.customer_name AS invoice_customer_name
        FROM public.work_orders w
        JOIN billing.invoices i ON i.doc_number = w.invoice_number
        WHERE w.billing_status = 'needs_classification'
        ORDER BY w.completed DESC NULLS LAST
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    wos = [dict(r) for r in cur.fetchall()]
    cur.close()

    print(f"Found {len(wos)} work orders to classify")

    if not wos:
        conn.close()
        return {"status": "nothing_to_classify", "classified": 0}

    access_token = refresh_qbo_token()

    classified = []
    errors = []
    by_payment_method = {"on_file": 0, "invoice": 0}
    by_category = {}
    unique_customers = set()

    for i, wo in enumerate(wos):
        try:
            result = classify_one(conn, wo, access_token, force_refresh_payment_methods)
            classified.append(result)
            by_payment_method[result["payment_method"]] = (
                by_payment_method.get(result["payment_method"], 0) + 1
            )
            cat = result["service_category"]
            by_category[cat] = by_category.get(cat, 0) + 1
            if result.get("customer_id"):
                unique_customers.add(result["customer_id"])

            if (i + 1) % 50 == 0:
                print(f"  ... {i + 1}/{len(wos)} classified")
        except Exception as e:
            err = f"{wo['wo_number']}: {e}"
            print(f"  ERROR: {err}")
            errors.append(err)

    conn.close()

    print(f"=== done: {len(classified)} classified, {len(errors)} errors ===")
    print(f"  by payment_method: {by_payment_method}")
    print(f"  by service_category: {by_category}")
    print(f"  unique customers: {len(unique_customers)}")

    return {
        "status": "success" if not errors else "partial",
        "classified": len(classified),
        "errors": errors[:20],
        "by_payment_method": by_payment_method,
        "by_service_category": by_category,
        "unique_customers": len(unique_customers),
        "sample": classified[:10],
    }
