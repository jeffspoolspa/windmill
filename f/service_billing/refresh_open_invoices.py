import requests
import wmill
import psycopg2
import psycopg2.extras
import time
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
QBO_IN_BATCH_SIZE = 400

# Retry policy: QBO realms share a per-minute rate limit. Under heavy activity
# the query endpoint returns 429 or 5xx, previously we logged and moved on —
# which silently left invoices with stale cached balances. Now we retry with
# backoff and RAISE if we can't recover, so the caller (scheduled cron or
# ad-hoc sync) knows the refresh didn't complete.
QBO_RETRY_ATTEMPTS = 5
QBO_RETRY_BACKOFF_CAP_S = 10


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


def _qbo_query_with_retry(url, headers, params):
    """GET with exponential backoff on 429/5xx/network errors.
    Honors Retry-After header when present (clamped to 10s).
    Raises RuntimeError if all attempts fail — callers decide whether that's
    fatal for the overall run.
    """
    last_err = None
    for attempt in range(QBO_RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = f"network: {e}"
            if attempt + 1 < QBO_RETRY_ATTEMPTS:
                time.sleep(min(0.5 * (2 ** attempt), QBO_RETRY_BACKOFF_CAP_S))
            continue

        if resp.ok:
            return resp

        # 4xx (other than 429) — definitive, no retry
        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            raise RuntimeError(f"QBO query {resp.status_code}: {resp.text[:300]}")

        # 429 / 5xx — retryable
        last_err = f"{resp.status_code}: {resp.text[:200]}"
        if attempt + 1 >= QBO_RETRY_ATTEMPTS:
            break
        ra = resp.headers.get("Retry-After")
        if ra and ra.isdigit():
            delay = min(int(ra), QBO_RETRY_BACKOFF_CAP_S)
        else:
            delay = min(0.5 * (2 ** attempt), QBO_RETRY_BACKOFF_CAP_S)
        time.sleep(delay)

    raise RuntimeError(f"QBO query exhausted {QBO_RETRY_ATTEMPTS} retries — last error: {last_err}")


def main(fail_on_partial: bool = False):
    """Refresh balance + email_status for open invoices only.

    Also auto-transitions billing.invoices.billing_status to 'processed' for
    invoices that QBO now shows as paid AND emailed — this handles the case
    where a customer pays + office emails outside our app.

    fail_on_partial: when True (e.g. if ever called from a preflight that
    needs all-or-nothing freshness), the whole run fails unless every batch
    came back cleanly. Default False for scheduled cron so one unrecoverable
    batch doesn't waste the rest of the work.
    """
    print("=== refresh_open_invoices started ===")
    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT doc_number FROM billing.invoices
        WHERE balance > 0
           OR balance IS NULL
           OR billing_status IN ('ready_to_process', 'needs_review', 'awaiting_pre_processing')
    """)
    doc_numbers = [r[0] for r in cur.fetchall()]
    print(f"Found {len(doc_numbers)} open / pre-terminal invoices to refresh")

    if not doc_numbers:
        cur.close()
        conn.close()
        return {"status": "nothing_to_refresh", "open_invoices": 0}

    access_token, realm_id = refresh_qbo_token()
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    qbo_data = {}
    failed_batches = []

    for i in range(0, len(doc_numbers), QBO_IN_BATCH_SIZE):
        batch = doc_numbers[i:i + QBO_IN_BATCH_SIZE]
        batch_num = i // QBO_IN_BATCH_SIZE + 1
        in_values = ", ".join([f"'{d}'" for d in batch])
        query = (f"SELECT DocNumber, Balance, EmailStatus, TotalAmt "
                 f"FROM Invoice WHERE DocNumber IN ({in_values}) MAXRESULTS 1000")
        try:
            resp = _qbo_query_with_retry(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
                headers=headers, params={"query": query},
            )
            for inv in resp.json().get("QueryResponse", {}).get("Invoice", []):
                doc_num = inv.get("DocNumber")
                if doc_num:
                    qbo_data[str(doc_num)] = {
                        "balance": float(inv.get("Balance", 0) or 0),
                        "email_status": inv.get("EmailStatus"),
                        "total_amt": float(inv.get("TotalAmt", 0) or 0),
                    }
            print(f"  batch {batch_num}: queried {len(batch)}, running total found {len(qbo_data)}")
        except RuntimeError as e:
            # Log & collect — decide whether to raise at the end based on
            # fail_on_partial. If we raise here, we lose the DB updates for
            # successful batches, which is worse than a partial refresh.
            print(f"  batch {batch_num} failed after retries: {e}")
            failed_batches.append({"batch": batch_num, "size": len(batch), "error": str(e)})

    now = datetime.now(timezone.utc)
    updated = 0
    newly_paid = 0

    for doc_number in doc_numbers:
        if doc_number in qbo_data:
            d = qbo_data[doc_number]
            cur.execute("""
                UPDATE billing.invoices
                SET balance = %s, email_status = %s, total_amt = %s, fetched_at = %s
                WHERE doc_number = %s
            """, (d["balance"], d["email_status"], d["total_amt"], now, doc_number))
            updated += 1
            if d["balance"] == 0:
                newly_paid += 1
    conn.commit()

    # Auto-transition invoices that are already done.
    #
    # Two independent completion signals:
    #   (a) Prior succeeded process_attempt — our own work log says done.
    #       Covers both charge path and invoice-email path regardless of
    #       balance (invoice-email-path invoices have balance > 0 by
    #       definition until the customer pays externally).
    #   (b) QBO externally shows balance=0 + EmailSent — customer paid +
    #       office emailed outside our app. Catches work done in QBO directly.
    #
    # NOTE: only safe to run if we had a clean pass. If some batches failed,
    # an invoice might incorrectly still show balance=0/EmailSent from an
    # OLD pull and get flipped based on stale data. Gate on clean batches.
    if not failed_batches:
        cur.execute("""
            UPDATE billing.invoices i
            SET billing_status = 'processed',
                processed_at = COALESCE(i.processed_at, now())
            WHERE i.billing_status IN ('ready_to_process', 'needs_review', 'awaiting_pre_processing')
              AND (
                EXISTS (
                  SELECT 1 FROM billing.processing_attempts pa
                  WHERE pa.qbo_invoice_id = i.qbo_invoice_id
                    AND pa.stage = 'process'
                    AND pa.status = 'succeeded'
                    AND pa.dry_run = false
                )
                OR (i.balance = 0 AND i.email_status = 'EmailSent')
              )
        """)
        auto_transitioned = cur.rowcount
        conn.commit()
    else:
        print(f"  skipping auto-transition: {len(failed_batches)} batch(es) failed, "
              f"state may be stale")
        auto_transitioned = 0

    cur.close()
    conn.close()

    print(f"=== done: {updated} refreshed, {newly_paid} newly paid, "
          f"{auto_transitioned} auto-transitioned, "
          f"{len(failed_batches)} batch(es) failed ===")

    # If the caller cares about all-or-nothing freshness, surface the failure.
    # The DB has been updated with whatever we did get, so they can decide
    # whether partial is acceptable.
    if failed_batches and fail_on_partial:
        raise RuntimeError(
            f"refresh_open_invoices: {len(failed_batches)} batch(es) failed "
            f"after retries — partial state in DB: {failed_batches}"
        )

    return {
        "status": "success" if not failed_batches else "partial",
        "open_invoices_checked": len(doc_numbers),
        "qbo_returned": len(qbo_data),
        "updated": updated,
        "newly_paid": newly_paid,
        "auto_transitioned_to_processed": auto_transitioned,
        "failed_batches": failed_batches,
    }
