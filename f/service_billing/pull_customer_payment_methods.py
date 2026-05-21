# Pull customer payment methods (cards + ACH) from QBO Payments API v4
# into billing.customer_payment_methods.
#
# Sweeps EVERY active customer with a QBO ID. The previous gate
# (`invoice_number IS NOT NULL` against billing.invoices) caused new
# cards/ACH to be invisible until the customer was first invoiced
# locally — sometimes weeks after they added the method in QBO. The
# table is meant to be a complete mirror of QBO Payments, not a slice
# driven by invoice activity.
#
# QBO Payments endpoints (same as before):
#   - GET /quickbooks/v4/customers/{id}/cards
#   - GET /quickbooks/v4/customers/{id}/bank-accounts
#
# TTL gate: we anchor on public."Customers".pm_last_checked_at, which is
# bumped after every successful QBO call (including the "no methods" case).
# A failed QBO call (non-2xx, timeout, network error) does NOT bump the
# timestamp, so it retries next sweep instead of silently looking like
# "this customer has no methods on file" for the next TTL window.
#
# Schedule: every 4 hours. With ~8.9k active customers and CACHE_TTL_MINUTES
# at 1440 (24h), each sweep refreshes roughly 1/6 of the base (~1.5k) — a
# 20-30 minute run. A force_refresh run will re-fetch everyone in one pass.

import requests
import wmill
import psycopg2
import psycopg2.extras
import uuid
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

# 24h. The sweep runs every 4h, so each cycle picks up the slice of
# customers that crossed the 24h mark since their last check — about
# 1/6 of the customer base per run. Total budget: every customer
# refreshed once a day. Tune lower if you need faster turnaround on
# QBO-side PM changes (and you've added concurrency / accepted longer
# per-sweep wall time).
CACHE_TTL_MINUTES = 1440


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


def fetch_methods_for_customer(customer_id: str, access_token: str):
    """Fetch cards + ACH for one customer from QBO Payments API v4.

    Returns (methods, fetch_errors). fetch_errors is a list of strings;
    when non-empty the caller should NOT mark the customer as checked
    (we don't know whether the empty methods list is real or just QBO
    being unhappy with us this minute).
    """
    methods: list[dict] = []
    errors: list[str] = []
    base_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # Cards
    try:
        r = requests.get(
            f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/cards",
            headers={**base_headers, "Request-Id": str(uuid.uuid4())},
            timeout=20,
        )
        if not r.ok:
            errors.append(f"cards HTTP {r.status_code}: {r.text[:300]}")
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
    except Exception as e:
        errors.append(f"cards exception: {e!r}")

    # ACH
    try:
        r = requests.get(
            f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/bank-accounts",
            headers={**base_headers, "Request-Id": str(uuid.uuid4())},
            timeout=20,
        )
        if not r.ok:
            errors.append(f"bank HTTP {r.status_code}: {r.text[:300]}")
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
    except Exception as e:
        errors.append(f"bank exception: {e!r}")

    return methods, errors


def main(force_refresh: bool = False, only_customer_id: str = ""):
    """Refresh customer payment methods.

    Args:
        force_refresh: If True, ignore TTL — re-fetch every eligible customer.
        only_customer_id: If non-empty, fetch just this one QBO customer ID
            (bypasses TTL too; useful for on-demand UI refresh).
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
        cur.execute("""
            SELECT qbo_customer_id FROM public."Customers"
            WHERE qbo_customer_id IS NOT NULL
              AND is_active = true
              AND deleted_at IS NULL
            ORDER BY pm_last_checked_at NULLS FIRST
        """)
        customer_ids = [r[0] for r in cur.fetchall()]
    else:
        cur.execute(
            """
            SELECT qbo_customer_id FROM public."Customers"
            WHERE qbo_customer_id IS NOT NULL
              AND is_active = true
              AND deleted_at IS NULL
              AND (pm_last_checked_at IS NULL
                   OR pm_last_checked_at < now() - (%s || ' minutes')::interval)
            ORDER BY pm_last_checked_at NULLS FIRST
            """,
            (CACHE_TTL_MINUTES,),
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
                # and don't bump pm_last_checked_at. Will retry next sweep.
                # Previously this looked identical to "customer has no
                # methods" and silently hid days of staleness.
                stats["fetch_errors"] += 1
                for err in fetch_errors:
                    print(f"  fetch_error {cid}: {err}")
                conn.commit()
                continue

            # Deactivate existing rows — methods still present in QBO get
            # flipped back to is_active=true by the upsert below; anything
            # QBO no longer returns stays deactivated.
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
            # customer goes unfetched — exactly how RIGBY's card got
            # missed for days. Roll back the aborted transaction, log,
            # and keep going.
            stats["exceptions"] += 1
            print(f"  ERROR on customer {cid}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            cur.close()
            cur = conn.cursor()

        if (i + 1) % 200 == 0:
            print(
                f"  ... {i + 1}/{len(customer_ids)} customers "
                f"(fetch_errors={stats['fetch_errors']}, exceptions={stats['exceptions']})"
            )

    cur.close()
    conn.close()

    print(f"=== done: {stats} ===")
    return {"status": "success", **stats}
