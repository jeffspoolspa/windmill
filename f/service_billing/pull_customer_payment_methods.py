# Pull customer payment methods (cards + ACH) from QBO Payments API v4
# into billing.customer_payment_methods.
#
# Two ways this script gets invoked:
#   1. AFTER INSERT trigger on billing.invoices fires a webhook with
#      only_customer_id=<NEW.qbo_customer_id> — the real-time path that
#      keeps PMs fresh for every open invoice (see migration
#      20260521000003_invoice_insert_triggers_pm_refresh.sql).
#   2. A daily scheduled run with no args — backstop that catches anything
#      the trigger missed (pg_net failure, Windmill outage, restored backup,
#      etc.) using the smart selection below.
#
# The smart selection scopes the sweep to customers who actually need a
# PM check — those with open billable invoices whose PM data is missing,
# stale, or pre-dates the most recent invoice. The previous "all 8.9k
# customers" approach was wasteful; the trigger handles the live signal,
# the daily backstop just sweeps a small remainder.
#
# QBO Payments endpoints (per-customer; QBO has no bulk endpoint):
#   - GET /quickbooks/v4/customers/{id}/cards
#   - GET /quickbooks/v4/customers/{id}/bank-accounts

import requests
import wmill
import psycopg2
import psycopg2.extras
import uuid
import time
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

# Daily-backstop safety net: even with no new invoices, refresh any open-
# invoice customer that hasn't been checked in this long. Catches the case
# where a card on QBO got removed without a corresponding invoice event.
BACKSTOP_TTL_INTERVAL = "1 day"


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


# 429 retry policy: when a burst of webhooks fires (e.g. an invoice sync
# inserts 30 invoices at once and pg_net fires 30 per-customer webhooks
# in parallel), QBO will rate-limit a handful. Without retry, those
# customers stay stale and their open invoices get blocked from
# ready_to_process by the freshness gate. Three exponential-backoff
# retries (0.5s, 1.5s, 4.5s) is enough to clear almost any burst.
RETRY_429_ATTEMPTS = 3
RETRY_429_BASE_DELAY = 0.5
RETRY_429_BACKOFF = 3.0


def _qbo_get_with_429_retry(url: str, base_headers: dict, label: str):
    """GET wrapper that retries HTTP 429 with exponential backoff.

    Returns (response, error_string_or_none). Caller checks error first;
    if None, response is a successful requests.Response.
    """
    last_status = None
    for attempt in range(RETRY_429_ATTEMPTS):
        try:
            r = requests.get(
                url,
                headers={**base_headers, "Request-Id": str(uuid.uuid4())},
                timeout=20,
            )
        except Exception as e:
            return None, f"{label} exception: {e!r}"

        if r.status_code != 429:
            if r.ok:
                return r, None
            return None, f"{label} HTTP {r.status_code}: {r.text[:300]}"

        last_status = r.status_code
        # Honor Retry-After if QBO sends one; otherwise use our backoff
        retry_after = r.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_for = float(retry_after)
        else:
            sleep_for = RETRY_429_BASE_DELAY * (RETRY_429_BACKOFF ** attempt)
        time.sleep(sleep_for)

    return None, f"{label} HTTP {last_status} after {RETRY_429_ATTEMPTS} retries"


def fetch_methods_for_customer(customer_id: str, access_token: str):
    """Fetch cards + ACH for one customer from QBO Payments API v4.

    Returns (methods, fetch_errors). When fetch_errors is non-empty the
    caller must NOT mark the customer as checked — we don't know whether
    the empty methods list is real or just QBO being unhappy with us.
    """
    methods: list[dict] = []
    errors: list[str] = []
    base_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # Cards
    r, err = _qbo_get_with_429_retry(
        f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/cards",
        base_headers, "cards",
    )
    if err:
        errors.append(err)
    else:
        cards = r.json() if isinstance(r.json(), list) else []
        for c in cards:
            if c.get("status") == "ACTIVE":
                methods.append({
                    "type": "credit_card",
                    "qbo_payment_method_id": c.get("id"),
                    "card_brand": c.get("cardType"),
                    "last_four": (c.get("number") or "")[-4:],
                    "is_default": bool(c.get("default")),
                    "raw": c,
                })

    # ACH
    r, err = _qbo_get_with_429_retry(
        f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/bank-accounts",
        base_headers, "bank",
    )
    if err:
        errors.append(err)
    else:
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

    return methods, errors


def main(force_refresh: bool = False, only_customer_id: str = ""):
    """Refresh customer payment methods.

    Args:
        force_refresh: If True, sweep ALL customers with open invoices
            ignoring TTL. Used to clear backlogs.
        only_customer_id: If non-empty, fetch just this one QBO customer ID
            (bypasses every filter). The trigger path uses this.

    Default (no args): smart-selection sweep over customers with open
    invoices where PM data is missing, stale, or pre-dates the most recent
    invoice. The daily backstop schedule calls this form.
    """
    print(
        f"=== pull_customer_payment_methods started "
        f"(force={force_refresh}, only={only_customer_id or '-'}) ==="
    )

    conn = get_db_conn()
    cur = conn.cursor()

    if only_customer_id:
        customer_ids = [only_customer_id]
    elif force_refresh:
        # Backlog clear — every customer with an open billable invoice,
        # regardless of pm_last_checked_at. Used to seed after deploys.
        cur.execute("""
            SELECT DISTINCT i.qbo_customer_id
              FROM billing.invoices i
              JOIN public."Customers" c ON c.qbo_customer_id = i.qbo_customer_id
             WHERE i.qbo_customer_id IS NOT NULL
               AND c.is_active = true
               AND c.deleted_at IS NULL
               AND i.billing_status != 'processed'
        """)
        customer_ids = [r[0] for r in cur.fetchall()]
    else:
        # Daily backstop selection: customers with open invoices whose PM
        # data is either missing, stale, or pre-dates their most-recent
        # invoice. The third predicate is what makes the trigger and the
        # sweep agree — if a trigger fired and succeeded, pm_last_checked_at
        # will be more recent than every invoice for that customer and the
        # sweep skips them. If the trigger missed (pg_net failure etc.),
        # the invoice's fetched_at is newer and the sweep catches up.
        cur.execute(
            f"""
            SELECT DISTINCT i.qbo_customer_id
              FROM billing.invoices i
              JOIN public."Customers" c ON c.qbo_customer_id = i.qbo_customer_id
             WHERE i.qbo_customer_id IS NOT NULL
               AND c.is_active = true
               AND c.deleted_at IS NULL
               AND i.billing_status != 'processed'
               AND (
                    c.pm_last_checked_at IS NULL
                    OR i.fetched_at > c.pm_last_checked_at
                    OR c.pm_last_checked_at < now() - interval '{BACKSTOP_TTL_INTERVAL}'
               )
            """
        )
        customer_ids = [r[0] for r in cur.fetchall()]

    cur.close()
    print(f"Found {len(customer_ids)} customers to fetch")

    if not customer_ids:
        conn.close()
        return {"status": "nothing_to_fetch", "customers": 0}

    access_token = refresh_qbo_token()
    now = datetime.now(timezone.utc)
    cur = conn.cursor()

    stats = {
        "customers": 0,
        "with_methods": 0,
        "total_methods": 0,
        "cards": 0,
        "ach": 0,
        "fetch_errors": 0,   # QBO call failed for this customer — will retry
        "exceptions": 0,     # Python/DB error escaped per-customer try
    }

    for i, cid in enumerate(customer_ids):
        try:
            methods, fetch_errors = fetch_methods_for_customer(cid, access_token)
            stats["customers"] += 1

            if fetch_errors:
                # Treat as "we couldn't ask QBO" — don't touch existing rows
                # and don't bump pm_last_checked_at. Customer naturally
                # retries on the next trigger or backstop. Previously this
                # looked identical to "customer has no methods" and silently
                # hid days of staleness.
                stats["fetch_errors"] += 1
                for err in fetch_errors:
                    print(f"  fetch_error {cid}: {err}")
                conn.commit()
                continue

            # Deactivate existing rows — methods still in QBO get flipped
            # back to is_active=true by the upsert below; anything QBO no
            # longer returns stays deactivated.
            cur.execute(
                "UPDATE billing.customer_payment_methods SET is_active = false WHERE qbo_customer_id = %s",
                (cid,),
            )

            if methods:
                stats["with_methods"] += 1
                for m in methods:
                    cur.execute(
                        """
                        INSERT INTO billing.customer_payment_methods
                            (qbo_customer_id, qbo_payment_method_id, type, card_brand,
                             last_four, is_default, is_active, raw, fetched_at)
                        VALUES (%s, %s, %s, %s, %s, %s, true, %s::jsonb, %s)
                        ON CONFLICT (qbo_customer_id, qbo_payment_method_id) DO UPDATE SET
                            type = EXCLUDED.type, card_brand = EXCLUDED.card_brand,
                            last_four = EXCLUDED.last_four, is_default = EXCLUDED.is_default,
                            is_active = true, raw = EXCLUDED.raw, fetched_at = EXCLUDED.fetched_at
                        """,
                        (
                            cid, m["qbo_payment_method_id"], m["type"],
                            m["card_brand"], m["last_four"], m["is_default"],
                            psycopg2.extras.Json(m.get("raw", {})), now,
                        ),
                    )
                    stats["total_methods"] += 1
                    if m["type"] == "credit_card":
                        stats["cards"] += 1
                    else:
                        stats["ach"] += 1

            # Bump the TTL anchor whether or not methods were returned.
            cur.execute(
                'UPDATE public."Customers" SET pm_last_checked_at = %s WHERE qbo_customer_id = %s',
                (now, cid),
            )
            conn.commit()
        except Exception as e:
            # Per-customer isolation. Without this, one bad row (an
            # unexpected QBO field, a constraint violation, a transient
            # DB blip) crashes the entire sweep and every downstream
            # customer goes unfetched.
            stats["exceptions"] += 1
            print(f"  ERROR on customer {cid}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            cur.close()
            cur = conn.cursor()

        if (i + 1) % 100 == 0:
            print(
                f"  ... {i + 1}/{len(customer_ids)} customers "
                f"(fetch_errors={stats['fetch_errors']}, exceptions={stats['exceptions']})"
            )

    cur.close()
    conn.close()

    print(f"=== done: {stats} ===")
    return {"status": "success", **stats}
