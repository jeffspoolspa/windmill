import requests
import wmill
import psycopg2
import psycopg2.extras
import uuid
import time
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
CACHE_TTL_MINUTES = 240

# Per-endpoint retry policy — cards/bank-accounts calls fail transiently under load
QBO_RETRY_ATTEMPTS = 3
QBO_RETRY_BACKOFF_S = 1.5


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


def _get_with_retry(url, access_token):
    """Hit a QBO Payments endpoint with exponential backoff.
    Returns (response_or_None, succeeded_bool).
    Succeeded=True ONLY if the final call returned 2xx — that's our contract
    for trusting the result enough to use it as grounds for deactivating
    stored methods. 404 counts as success (customer has no methods of this
    type) but network / 5xx / 429 exhaust the retries and return False.
    """
    last_err = None
    for attempt in range(QBO_RETRY_ATTEMPTS):
        headers = {"Authorization": f"Bearer {access_token}",
                   "Accept": "application/json",
                   "Request-Id": str(uuid.uuid4())}
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = f"network: {e}"
        else:
            if r.ok or r.status_code == 404:
                return r, True
            # 429 / 5xx / other transient — retry
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"{r.status_code}: {r.text[:120]}"
            else:
                # 4xx that isn't 429 — definitive failure, don't retry
                return r, False
        if attempt + 1 < QBO_RETRY_ATTEMPTS:
            time.sleep(QBO_RETRY_BACKOFF_S * (2 ** attempt))
    print(f"  retry exhausted for {url}: {last_err}")
    return None, False


def fetch_methods_for_customer(customer_id, access_token):
    """Returns (methods, fetch_fully_ok).

    fetch_fully_ok=True ONLY when BOTH the cards AND bank-accounts queries
    completed successfully (or returned 404). This is the gate on whether
    we trust the result enough to deactivate stored methods — previous
    bug: silent fetch errors returned empty methods, which then blanket-
    deactivated real cards. Now the caller only deactivates when
    fetch_fully_ok is True.
    """
    methods = []
    cards_url = f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/cards"
    banks_url = f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/bank-accounts"

    cards_resp, cards_ok = _get_with_retry(cards_url, access_token)
    if cards_ok and cards_resp is not None and cards_resp.status_code == 200:
        try:
            body = cards_resp.json()
            for c in (body if isinstance(body, list) else []):
                if c.get("status") == "ACTIVE":
                    methods.append({
                        "type": "card",
                        "qbo_payment_method_id": c.get("id"),
                        "card_brand": c.get("cardType"),
                        "last_four": (c.get("number") or "")[-4:],
                        "is_default": bool(c.get("default")),
                        "raw": c,
                    })
        except ValueError as e:
            print(f"  card parse error for {customer_id}: {e}")
            cards_ok = False

    banks_resp, banks_ok = _get_with_retry(banks_url, access_token)
    if banks_ok and banks_resp is not None and banks_resp.status_code == 200:
        try:
            body = banks_resp.json()
            for b in (body if isinstance(body, list) else []):
                if b.get("verificationStatus") in ("VERIFIED", "NOT_VERIFIED"):
                    methods.append({
                        "type": "ach",
                        "qbo_payment_method_id": b.get("id"),
                        "card_brand": b.get("bankName"),
                        "last_four": (b.get("accountNumber") or "")[-4:],
                        "is_default": bool(b.get("default")),
                        "raw": b,
                    })
        except ValueError as e:
            print(f"  bank parse error for {customer_id}: {e}")
            banks_ok = False

    fetch_fully_ok = cards_ok and banks_ok
    return methods, fetch_fully_ok


def main(force_refresh: bool = False, customer_ids: list = None):
    """
    Pull QBO customer payment methods (cards + ACH) into billing.customer_payment_methods.

    Args:
      force_refresh: ignore the CACHE_TTL_MINUTES window, re-fetch every customer.
      customer_ids: if passed, ONLY fetch these customers (used for targeted
                    single-customer refreshes from process_invoice or manual recovery).
    """
    print(f"=== pull_customer_payment_methods (force={force_refresh}, "
          f"scoped={customer_ids is not None}) ===")
    conn = get_db_conn()
    cur = conn.cursor()

    if customer_ids:
        target_ids = [str(c) for c in customer_ids if c]
    elif force_refresh:
        cur.execute("""
            SELECT DISTINCT i.qbo_customer_id FROM public.work_orders w
            JOIN billing.invoices i ON i.doc_number = w.invoice_number
            WHERE w.invoice_number IS NOT NULL AND i.qbo_customer_id IS NOT NULL
        """)
        target_ids = [r[0] for r in cur.fetchall()]
    else:
        cur.execute(f"""
            SELECT DISTINCT i.qbo_customer_id FROM public.work_orders w
            JOIN billing.invoices i ON i.doc_number = w.invoice_number
            WHERE w.invoice_number IS NOT NULL AND i.qbo_customer_id IS NOT NULL
              AND i.qbo_customer_id NOT IN (
                SELECT DISTINCT qbo_customer_id FROM billing.customer_payment_methods
                WHERE fetched_at > now() - interval '{CACHE_TTL_MINUTES} minutes'
              )
        """)
        target_ids = [r[0] for r in cur.fetchall()]
    cur.close()
    print(f"Target: {len(target_ids)} customer(s)")

    if not target_ids:
        conn.close()
        return {"status": "nothing_to_fetch", "customers": 0}

    access_token = refresh_qbo_token()
    now = datetime.now(timezone.utc)
    cur = conn.cursor()
    stats = {"customers": 0, "with_methods": 0, "total_methods": 0,
             "cards": 0, "ach": 0, "default_cards": 0, "default_ach": 0,
             "skipped_fetch_error": 0}

    for i, cid in enumerate(target_ids):
        methods, fetch_ok = fetch_methods_for_customer(cid, access_token)
        stats["customers"] += 1

        if not fetch_ok:
            # SAFE MODE: we didn't get a clean answer from QBO. Do NOT blanket-
            # deactivate this customer's stored methods — that would nuke real
            # data on a transient failure. Log and move on; they'll be retried
            # next run (or via targeted customer_ids=[cid] call).
            stats["skipped_fetch_error"] += 1
            print(f"  customer {cid}: fetch incomplete, leaving existing rows untouched")
            conn.commit()
            continue

        # Fetch was clean → upsert what we got and deactivate anything not present.
        # Two-step: first INSERT/UPDATE the live set (marks them is_active=true),
        # then UPDATE is_active=false for anything fetched_at is older than `now`.
        live_ids = []
        if methods:
            stats["with_methods"] += 1
            for m in methods:
                cur.execute("""
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
                """, (cid, m["qbo_payment_method_id"], m["type"], m["card_brand"],
                      m["last_four"], m["is_default"],
                      psycopg2.extras.Json(m.get("raw", {})), now))
                live_ids.append(m["qbo_payment_method_id"])
                stats["total_methods"] += 1
                if m["type"] == "card":
                    stats["cards"] += 1
                    if m["is_default"]:
                        stats["default_cards"] += 1
                else:
                    stats["ach"] += 1
                    if m["is_default"]:
                        stats["default_ach"] += 1

        # Deactivate methods we DIDN'T see in this successful fetch.
        if live_ids:
            cur.execute(
                "UPDATE billing.customer_payment_methods "
                "SET is_active = false "
                "WHERE qbo_customer_id = %s "
                "  AND qbo_payment_method_id NOT IN %s "
                "  AND is_active = true",
                (cid, tuple(live_ids)),
            )
        else:
            # Customer truly has no active methods right now — deactivate all.
            cur.execute(
                "UPDATE billing.customer_payment_methods "
                "SET is_active = false "
                "WHERE qbo_customer_id = %s AND is_active = true",
                (cid,),
            )
        conn.commit()
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(target_ids)} customers")

    cur.close()
    conn.close()
    print(f"=== done: {stats} ===")
    return {"status": "success", **stats}
