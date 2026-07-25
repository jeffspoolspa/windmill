# requirements:
# psycopg2-binary
# requests

"""
f/billing/_lib/payment_methods — the customer's wallet.

SOLE AUTHOR of billing.customer_payment_methods. Two scripts used to write
this table with their own copies of the same INSERT
(pull_customer_payment_methods, classify_work_orders); divergence there means
the same wallet is recorded two different ways.

The wallet lives on the QBO PAYMENTS API (api.intuit.com/quickbooks/v4) — a
different host and shape from the accounting API in _lib/qbo, which is why it
is its own module rather than a QboClient method.

Import as:  from f.billing._lib import payment_methods
"""

import time

import psycopg2.extras
import requests

from f.billing._lib.db import query_one, execute

BASE = "https://api.intuit.com/quickbooks/v4/customers"

# QBO Payments sends NO webhooks, and the daily sweep in
# pull_customer_payment_methods selects customers by joining billing.invoices —
# so it structurally cannot see a customer at the moment an invoice is BORN,
# which is exactly when pre-processing decides whether to charge them. That
# makes the refresh inside payment_route the only sync point that runs at the
# right time, and this TTL the only thing standing between routing and a stale
# wallet. It exists solely to amortise a burst (one drain, many invoices for
# one customer) — NOT to save API calls in general. Keep it near a drain's
# length, not near an hour.
DEFAULT_MAX_AGE_MINUTES = 15


def _get(url, access_token, label, max_retries=3):
    """GET with 429 backoff. Returns (payload, error) — never raises, because
    the caller must be able to tell "QBO said no methods" apart from "QBO
    would not answer"."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            return None, f"{label}: {type(e).__name__}: {e}"
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            return None, f"{label}: HTTP {r.status_code}: {r.text[:200]}"
        try:
            body = r.json()
        except ValueError:
            return None, f"{label}: response was not JSON"
        return (body if isinstance(body, list) else []), None
    return None, f"{label}: rate limited after {max_retries} attempts"


def fetch(qbo_customer_id, access_token):
    """Cards + verified bank accounts for one customer, straight from QBO.

    Returns (methods, errors). A non-empty `errors` means we do NOT know the
    customer's wallet — the caller must not treat the empty list as truth.
    """
    methods, errors = [], []

    cards, err = _get(f"{BASE}/{qbo_customer_id}/cards", access_token, "cards")
    if err:
        errors.append(err)
    else:
        methods += [{"type": "credit_card",
                     "qbo_payment_method_id": c.get("id"),
                     "card_brand": c.get("cardType"),
                     "last_four": (c.get("number") or "")[-4:], "raw": c}
                    for c in cards if c.get("status") == "ACTIVE"]

    banks, err = _get(f"{BASE}/{qbo_customer_id}/bank-accounts", access_token, "bank")
    if err:
        errors.append(err)
    else:
        methods += [{"type": "ach",
                     "qbo_payment_method_id": b.get("id"),
                     "card_brand": b.get("bankName"),
                     "last_four": (b.get("accountNumber") or "")[-4:], "raw": b}
                    for b in banks if b.get("verificationStatus")
                    in ("VERIFIED", "NOT_VERIFIED")]

    # NOTE: QBO's own `default` flag is deliberately NOT read into a column.
    # billing.fn_maintain_default_pm owns is_default (newest active per
    # customer) — the Country Inn lesson. The flag stays visible in `raw` for
    # audit, but nothing routes off it.
    return methods, errors


def is_stale(conn, qbo_customer_id, max_age_minutes=DEFAULT_MAX_AGE_MINUTES):
    row = query_one(conn, """
        SELECT pm_last_checked_at IS NULL
               OR pm_last_checked_at < now() - make_interval(mins => %s) AS stale
          FROM public."Customers" WHERE qbo_customer_id = %s""",
        (max_age_minutes, qbo_customer_id))
    return True if row is None else bool(row["stale"])


def replace(conn, qbo_customer_id, methods):
    """Converge the cache to exactly what QBO returned.

    Deactivates only the methods QBO no longer lists — never all of them.
    The obvious "deactivate everything, then reactivate what came back" makes
    billing.fn_maintain_default_pm briefly see a customer with NO active
    method, so it clears the default and emits payment_method_default_changed
    -> null, then emits again on reinsert. Two false events on the customer's
    history every refresh, for a wallet that did not change.

    Rows are deactivated, never deleted, so charge history survives.

    is_default is NOT written here. fn_maintain_default_pm is that column's
    sole author; writing QBO's flag would just be overwritten by it, and for
    a moment the row would claim a default we do not believe.
    """
    execute(conn, """
        UPDATE billing.customer_payment_methods SET is_active = false
         WHERE qbo_customer_id = %s AND is_active = true
           AND qbo_payment_method_id <> ALL(%s)""",
        (qbo_customer_id, [m["qbo_payment_method_id"] for m in methods]))
    for m in methods:
        execute(conn, """
            INSERT INTO billing.customer_payment_methods
                (qbo_customer_id, qbo_payment_method_id, type, card_brand,
                 last_four, is_default, is_active, raw, fetched_at)
            VALUES (%s, %s, %s, %s, %s, false, true, %s::jsonb, now())
            ON CONFLICT (qbo_customer_id, qbo_payment_method_id) DO UPDATE SET
                type = EXCLUDED.type, card_brand = EXCLUDED.card_brand,
                last_four = EXCLUDED.last_four,
                is_active = true, raw = EXCLUDED.raw, fetched_at = now()""",
            (qbo_customer_id, m["qbo_payment_method_id"], m["type"],
             m["card_brand"], m["last_four"],
             psycopg2.extras.Json(m.get("raw", {}))))
    execute(conn, 'UPDATE public."Customers" SET pm_last_checked_at = now() '
                  "WHERE qbo_customer_id = %s", (qbo_customer_id,))
    conn.commit()


def refresh(conn, qbo_customer_id, access_token,
            max_age_minutes=DEFAULT_MAX_AGE_MINUTES):
    """Converge the wallet before anyone routes off it. Skips if already
    fresh. Raises if QBO would not answer — routing a payment off a wallet we
    could not verify is exactly the quiet wrongness this refactor removes.

    Ordering is guaranteed, not incidental: trg_maintain_default_pm is an
    AFTER ROW trigger, so Postgres runs it inside `replace`'s transaction
    before the INSERT returns. By the time this function returns, is_default
    is settled and billing.pick_target_payment_method reads a decided table.
    Nothing here is pg_net-style async."""
    if not is_stale(conn, qbo_customer_id, max_age_minutes):
        return {"refreshed": False, "reason": "fresh"}
    methods, errors = fetch(qbo_customer_id, access_token)
    if errors:
        raise RuntimeError(f"payment methods unreadable for {qbo_customer_id}: "
                           f"{'; '.join(errors)}")
    replace(conn, qbo_customer_id, methods)
    return {"refreshed": True, "methods": len(methods)}


# ── self-check: no DB, no network ───────────────────────────────────────────

def main():
    checks = []
    def ok(n, c): checks.append((n, bool(c)))

    global _get
    real_get = _get

    def stub(payloads):
        def _stubbed(url, token, label, max_retries=3):
            value = payloads.get(label)
            return (None, value) if isinstance(value, str) else (value, None)
        return _stubbed

    try:
        # a normal wallet: one active card, one expired card, one verified bank
        _get = stub({"cards": [{"id": "c1", "status": "ACTIVE", "cardType": "Visa",
                                "number": "xxxx4242", "default": True},
                               {"id": "c2", "status": "EXPIRED", "cardType": "Visa",
                                "number": "xxxx1111", "default": False}],
                     "bank": [{"id": "b1", "verificationStatus": "VERIFIED",
                               "bankName": "Chase", "accountNumber": "xxxx9999",
                               "default": False}]})
        methods, errors = fetch("C1", "tok")
        ok("no errors on a clean fetch", errors == [])
        ok("expired card excluded", len(methods) == 2)
        ok("card parsed", methods[0] == {"type": "credit_card",
            "qbo_payment_method_id": "c1", "card_brand": "Visa",
            "last_four": "4242", "raw": methods[0]["raw"]})
        ok("QBO's default flag is NOT surfaced as a field "
           "(fn_maintain_default_pm owns is_default)",
           all("is_default" not in m for m in methods))
        ok("but QBO's flag stays visible in raw for audit",
           methods[0]["raw"]["default"] is True)
        ok("bank parsed as ach", methods[1]["type"] == "ach"
           and methods[1]["last_four"] == "9999")

        # a customer with genuinely nothing
        _get = stub({"cards": [], "bank": []})
        methods, errors = fetch("C2", "tok")
        ok("empty wallet is not an error", methods == [] and errors == [])

        # THE distinction that matters: QBO refusing != customer has none
        _get = stub({"cards": "cards: HTTP 500", "bank": []})
        methods, errors = fetch("C3", "tok")
        ok("a failed fetch reports an error", len(errors) == 1)
        ok("a failed fetch must not look like an empty wallet",
           methods == [] and errors != [])

        # unverified bank accounts are still chargeable; unusable ones are not
        _get = stub({"cards": [], "bank": [
            {"id": "b1", "verificationStatus": "NOT_VERIFIED", "bankName": "X",
             "accountNumber": "1234", "default": False},
            {"id": "b2", "verificationStatus": "FAILED", "bankName": "Y",
             "accountNumber": "5678", "default": False}]})
        methods, _ = fetch("C4", "tok")
        ok("unverified kept, failed dropped",
           [m["qbo_payment_method_id"] for m in methods] == ["b1"])
    finally:
        _get = real_get

    failed = [n for n, p in checks if not p]
    return {"ok": not failed, "passed": len(checks) - len(failed),
            "total": len(checks), "failed": failed}


if __name__ == "__main__":
    print(main())
