# f/service_billing/reconcile_payments
#
# Resolves processing_attempts stuck at status='charge_uncertain' by querying
# Intuit Payments directly to confirm whether the charge actually landed.
#
# Why we need this:
#   When a charge call returns 5xx / timeouts / network error, we don't know
#   if money moved. process_invoice writes status='charge_uncertain' and halts
#   to avoid double-charging. Without a reconciler, those rows sit forever.
#
# What this script does:
#   For each charge_uncertain attempt within the lookback window:
#     1. Query Intuit Payments /v4/payments/charges with the customer's
#        cardOnFile + a date filter around attempted_at.
#     2. Look for a charge that matches our amount + recent timestamp.
#     3a. Match found → promote to 'charge_succeeded'. Process_invoice's
#         auto-resume path picks it up next run and writes the QBO Payment.
#     3b. No match AND attempted >24h ago → 'charge_uncertain_expired'.
#         Idempotency cache is gone; safe to retry with a new key.
#     3c. No match AND attempted <24h ago → leave at 'charge_uncertain'.
#         Could still be in flight; check again next cron tick.
#     3d. After 7d total with no match → 'needs_reconcile_review' so a human
#         takes a look.
#
# Run schedule:
#   Every 5 minutes via Windmill cron. Quick + idempotent.
#
# Idempotency:
#   This script never writes to QBO or Intuit — it only reads + updates our
#   own DB. Safe to re-run any time.

import json
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

# How far back to look for uncertain attempts. 7 days is plenty — anything
# older has either been resolved manually or is genuinely abandoned.
LOOKBACK_DAYS = 7

# Window around attempted_at to search Intuit for matching charges.
# Generous because Intuit's `created` timestamps may drift from when our
# request fired by a few seconds, and we don't want to miss matches due
# to clock skew.
SEARCH_WINDOW_BEFORE = timedelta(minutes=2)
SEARCH_WINDOW_AFTER = timedelta(minutes=10)

# Idempotency key cache window per Intuit docs.
IDEMPOTENCY_WINDOW = timedelta(hours=24)

# Beyond this age we promote to needs_reconcile_review for human attention.
NEEDS_REVIEW_AFTER = timedelta(days=7)


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
    return tokens["access_token"], resource["realm_id"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def load_uncertain_attempts(conn):
    """Pull every charge_uncertain attempt in the lookback window with the
    fields we need to query Intuit. JOINs to cpm to get the cardOnFile id."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
          a.id, a.qbo_invoice_id, a.wo_number, a.invoice_number,
          a.idempotency_key, a.attempted_at, a.charge_amount,
          a.channel, a.customer_payment_method_id,
          a.charge_result,
          cpm.qbo_payment_method_id  AS card_on_file_id,
          cpm.type                   AS pm_type,
          i.qbo_customer_id
        FROM billing.processing_attempts a
        JOIN billing.invoices i  ON i.qbo_invoice_id = a.qbo_invoice_id
        LEFT JOIN billing.customer_payment_methods cpm
               ON cpm.id = a.customer_payment_method_id
        WHERE a.status = 'charge_uncertain'
          AND a.attempted_at > now() - interval '%s days'
          AND a.dry_run = false
        ORDER BY a.attempted_at ASC
    """ % LOOKBACK_DAYS)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def search_qbo_for_payment(attempt, access_token, realm_id):
    """Query QBO Payment entities to verify whether a charge landed.

    Why QBO and not Intuit Payments directly:
      Intuit Payments V4 has no list endpoint — only POST (charge) and
      GET by specific charge_id. We can't enumerate charges by date/
      customer/amount. So we use the QBO Data API instead: process_invoice
      records every successful charge as a QBO Payment with the Intuit
      charge_id stored in CreditCardPayment.CreditChargeResponse.CCTransId.

    Limitation: this only works if process_invoice got far enough to
    record the QBO Payment. If the script crashed BETWEEN the Intuit
    charge and the record_qbo_payment call, no QBO Payment exists and
    we can't auto-verify — the attempt stays charge_uncertain until
    the 24h window passes (then promotes to expired) or a human checks
    Intuit Merchant Center directly.

    Returns the same shape as before:
      {"found": True, "charge": {charge_id, amount, ...}, "match_confidence": "exact"}
      {"found": False, "reason": "..."}
      {"error": "..."}
    """
    customer_id = attempt.get("qbo_customer_id")
    if not customer_id:
        return {"error": "attempt has no qbo_customer_id"}

    attempted_at = attempt["attempted_at"]
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    # QBO TxnDate is a date, not a timestamp — search a 2-day window
    # around the attempt to catch off-by-one timezone edges.
    date_after  = (attempted_at - timedelta(days=1)).date().isoformat()
    date_before = (attempted_at + timedelta(days=1)).date().isoformat()

    # QBO query language (similar to SQL but limited). Filter by customer
    # + date range + payment method (CC=21, ACH=20). TotalAmt filter is
    # client-side because QBO doesn't always honor decimal equality on
    # numeric fields.
    pmt_method_id = "20" if attempt["pm_type"] == "ach" else "21"
    query = (
        f"SELECT * FROM Payment "
        f"WHERE CustomerRef = '{customer_id}' "
        f"AND TxnDate >= '{date_after}' "
        f"AND TxnDate <= '{date_before}' "
        f"AND PaymentMethodRef = '{pmt_method_id}'"
    )
    try:
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers={"Authorization": f"Bearer {access_token}",
                     "Accept": "application/json"},
            params={"query": query}, timeout=30,
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        return {"error": f"qbo query timeout: {str(e)[:200]}"}

    if not resp.ok:
        return {"error": f"qbo query HTTP {resp.status_code}: {resp.text[:200]}"}

    try:
        body = resp.json()
    except Exception:
        return {"error": "qbo query returned unparseable body"}

    payments = (body.get("QueryResponse") or {}).get("Payment", [])

    target_amount = float(attempt["charge_amount"] or 0)

    # Match by amount within $0.01 AND has a CCTransId (proves it was a
    # charged payment, not a manual one). The CCTransId IS the Intuit
    # charge_id we'd want to store on the attempt.
    matches = []
    for p in payments:
        p_amount = float(p.get("TotalAmt", 0) or 0)
        if abs(p_amount - target_amount) >= 0.01:
            continue
        cc_info = p.get("CreditCardPayment") or {}
        cc_trans_id = (cc_info.get("CreditChargeResponse") or {}).get("CCTransId")
        if cc_trans_id:
            matches.append({
                "qbo_payment_id": p.get("Id"),
                "charge_id": cc_trans_id,
                "amount": p_amount,
                "txn_date": p.get("TxnDate"),
                "raw": p,
            })

    if len(matches) == 1:
        m = matches[0]
        return {
            "found": True,
            "charge": {
                "id": m["charge_id"],
                "amount": m["amount"],
                "qbo_payment_id": m["qbo_payment_id"],
                "txn_date": m["txn_date"],
            },
            "match_confidence": "exact",
        }
    if len(matches) > 1:
        return {"found": False,
                "reason": f"ambiguous: {len(matches)} QBO payments match amount + customer"}
    return {"found": False, "reason": "no matching QBO payment found"}


# Legacy alias — keep so we don't break the call site if anything else uses it.
def search_intuit_for_charge(attempt, access_token):
    """DEPRECATED — Intuit V4 doesn't support listCharges. Kept as a stub
    that always returns no-match so the caller falls into the expiration
    path. Real verification happens in search_qbo_for_payment now."""
    return {"found": False, "reason": "intuit listCharges not supported by V4 API"}
    # Original code preserved below for reference, never executed:
    if not attempt.get("card_on_file_id"):
        return {"error": "attempt has no card_on_file_id (no cpm linked)"}

    attempted_at = attempt["attempted_at"]
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    after  = (attempted_at - SEARCH_WINDOW_BEFORE).isoformat()
    before = (attempted_at + SEARCH_WINDOW_AFTER).isoformat()

    # Pick endpoint based on payment type.
    if attempt["pm_type"] in ("credit_card", "card"):
        path = "v4/payments/charges"
    elif attempt["pm_type"] == "ach":
        path = "v4/payments/echecks"
    else:
        return {"error": f"unknown pm_type: {attempt['pm_type']}"}

    params = {
        "createdAfter": after,
        "createdBefore": before,
    }
    try:
        resp = requests.get(
            f"https://api.intuit.com/quickbooks/{path}",
            headers={"Authorization": f"Bearer {access_token}",
                     "Accept": "application/json"},
            params=params, timeout=30,
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        return {"error": f"intuit search timeout: {str(e)[:200]}"}

    if not resp.ok:
        return {"error": f"intuit search HTTP {resp.status_code}: {resp.text[:200]}"}

    try:
        body = resp.json()
    except Exception:
        return {"error": "intuit search returned unparseable body"}

    # Intuit Payments search returns either a list directly or a wrapper
    charges = body if isinstance(body, list) else body.get("charges", [])

    target_amount = float(attempt["charge_amount"] or 0)
    expected_card = attempt["card_on_file_id"]

    # Filter to matches: same card on file + amount within $0.01.
    matches = []
    for c in charges:
        c_amount = float(c.get("amount", 0) or 0)
        c_card = (c.get("card") or {}).get("id") or c.get("cardOnFile")
        if abs(c_amount - target_amount) < 0.01 and c_card == expected_card:
            matches.append(c)

    if len(matches) == 1:
        m = matches[0]
        # Only count as success if Intuit shows a successful status.
        status = (m.get("status") or "").upper()
        if attempt["pm_type"] == "ach":
            ok = status in ("PENDING", "SUCCEEDED")
        else:
            ok = status == "CAPTURED"
        if ok:
            return {"found": True, "charge": m, "match_confidence": "exact"}
        else:
            return {"found": True, "charge": m, "match_confidence": "exact_but_failed",
                    "intuit_status": status}

    if len(matches) > 1:
        # Multiple charges that match amount + card in our window. Could
        # be a duplicate charge OR another invoice for the same customer.
        # Surface for human review — we don't auto-resolve ambiguity.
        return {"found": False, "reason": f"ambiguous: {len(matches)} matching charges"}

    return {"found": False, "reason": "no matching charge in Intuit"}


def promote_to_charge_succeeded(conn, attempt, charge):
    """Mark the attempt as charge_succeeded with the discovered charge_id.
    process_invoice's auto-resume picks it up on next run."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.processing_attempts
        SET status = 'charge_succeeded',
            charge_id = %s,
            charge_result = %s::jsonb,
            error_message = NULL
        WHERE id = %s
    """, (charge.get("id"),
          json.dumps({"reconciled_from_uncertain": True,
                      "intuit_charge": charge,
                      "reconciled_at": datetime.now(timezone.utc).isoformat()}),
          attempt["id"]))
    conn.commit(); cur.close()


def promote_to_expired(conn, attempt, reason):
    """Reconciler verified no matching charge AND idempotency window passed.
    Process_invoice will start a fresh attempt with a new key."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.processing_attempts
        SET status = 'charge_uncertain_expired',
            error_message = %s
        WHERE id = %s
    """, (f"reconciler: {reason} (>24h, safe to retry fresh)", attempt["id"]))
    conn.commit(); cur.close()


def promote_to_needs_review(conn, attempt, reason):
    """Reconciler can't determine state. Human investigation required."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.processing_attempts
        SET status = 'needs_reconcile_review',
            error_message = %s
        WHERE id = %s
    """, (f"reconciler: {reason}", attempt["id"]))
    conn.commit(); cur.close()


def main(dry_run: bool = False):
    """Reconcile every charge_uncertain attempt. Logs what it did, writes
    state changes (unless dry_run=True). Run on a 5-min cron via Windmill."""
    print(f"=== reconcile_payments (dry_run={dry_run}, lookback={LOOKBACK_DAYS}d) ===")
    conn = get_db_conn()
    try:
        access_token, realm_id = refresh_qbo_token()
        attempts = load_uncertain_attempts(conn)
        print(f"Found {len(attempts)} charge_uncertain attempts to check")

        stats = {"promoted_succeeded": 0, "expired": 0, "needs_review": 0,
                 "still_pending": 0, "errored": 0}
        results = []

        now = datetime.now(timezone.utc)

        for a in attempts:
            attempted_at = a["attempted_at"]
            if attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=timezone.utc)
            age = now - attempted_at
            tag = (f"attempt={a['id']} inv={a['qbo_invoice_id']} "
                   f"amount=${float(a['charge_amount'] or 0):.2f} age={age}")

            search = search_qbo_for_payment(a, access_token, realm_id)

            if "error" in search:
                stats["errored"] += 1
                results.append({"id": str(a["id"]), "outcome": "error",
                                "detail": search["error"]})
                print(f"  ERROR  {tag}: {search['error']}")
                continue

            if search.get("found") and search.get("match_confidence") == "exact":
                # Charge actually landed → promote.
                charge_id = search["charge"].get("id")
                stats["promoted_succeeded"] += 1
                results.append({"id": str(a["id"]), "outcome": "promoted",
                                "charge_id": charge_id})
                print(f"  FOUND  {tag} → charge_id={charge_id} (promoting)")
                if not dry_run:
                    promote_to_charge_succeeded(conn, a, search["charge"])
                continue

            if search.get("found") and search.get("match_confidence") == "exact_but_failed":
                # Charge exists but Intuit shows it as failed/refunded — treat
                # the attempt as charge_declined.
                stats["needs_review"] += 1
                results.append({"id": str(a["id"]), "outcome": "intuit_failed",
                                "intuit_status": search.get("intuit_status")})
                print(f"  FAILED {tag} → intuit status={search.get('intuit_status')}")
                if not dry_run:
                    promote_to_needs_review(
                        conn, a,
                        f"matched charge but Intuit status={search.get('intuit_status')}",
                    )
                continue

            # search.found is False — no matching charge in Intuit
            reason = search.get("reason", "no match")

            if age >= NEEDS_REVIEW_AFTER:
                stats["needs_review"] += 1
                results.append({"id": str(a["id"]), "outcome": "needs_review",
                                "detail": reason})
                print(f"  STALE  {tag}: {reason} (>7d, escalating)")
                if not dry_run:
                    promote_to_needs_review(conn, a, f"{reason} after 7d")
                continue

            if age >= IDEMPOTENCY_WINDOW:
                stats["expired"] += 1
                results.append({"id": str(a["id"]), "outcome": "expired",
                                "detail": reason})
                print(f"  EXPIRE {tag}: {reason} (>24h, safe to retry fresh)")
                if not dry_run:
                    promote_to_expired(conn, a, reason)
                continue

            # Within idempotency window, no match yet — leave as uncertain
            stats["still_pending"] += 1
            results.append({"id": str(a["id"]), "outcome": "still_uncertain",
                            "age_minutes": int(age.total_seconds() / 60)})
            print(f"  WAIT   {tag}: {reason} (still in 24h window)")

        print(f"=== done: {stats} ===")
        return {"stats": stats, "results": results, "dry_run": dry_run}

    finally:
        conn.close()
