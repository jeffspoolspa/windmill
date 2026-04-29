import requests
import wmill
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
LOOKBACK_DAYS = 180


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


def qbo_query_all(query, entity, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    all_results = []
    start = 1
    while True:
        paged = f"{query} STARTPOSITION {start} MAXRESULTS 1000"
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers, params={"query": paged}, timeout=60,
        )
        if not resp.ok:
            print(f"  QBO query failed at pos {start}: {resp.status_code}")
            break
        batch = resp.json().get("QueryResponse", {}).get(entity, [])
        all_results.extend(batch)
        print(f"  page {(start - 1) // 1000 + 1}: {len(batch)} {entity}s (total {len(all_results)})")
        if len(batch) < 1000:
            break
        start += 1000
    return all_results


def fetch_payment_method_map(access_token, realm_id):
    """One-shot PaymentMethod entity query -> {id: name}.

    QBO's Payment query returns PaymentMethodRef.value but NOT .name, so we
    have to resolve names separately. There are usually only ~15-30 methods
    total (Check, Cash, Credit Card, Discover, MasterCard, ACH, etc.), so
    this is a single cheap query.
    """
    methods = qbo_query_all("SELECT * FROM PaymentMethod", "PaymentMethod", access_token, realm_id)
    return {m.get("Id"): m.get("Name") for m in methods if m.get("Id")}


def upsert_payment(cur, row, now):
    """Upsert into billing.customer_payments.
    Keeps fully-applied rows as history (don't delete -- we need them for
    the applied-payments UI and reconciliation).
    """
    cur.execute("""
        INSERT INTO billing.customer_payments
            (qbo_payment_id, qbo_customer_id, type, unapplied_amt,
             total_amt, txn_date, ref_num, memo,
             payment_method_id, payment_method_name,
             raw, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (qbo_payment_id) DO UPDATE SET
            unapplied_amt = EXCLUDED.unapplied_amt, total_amt = EXCLUDED.total_amt,
            txn_date = EXCLUDED.txn_date, ref_num = EXCLUDED.ref_num,
            memo = EXCLUDED.memo,
            payment_method_id = EXCLUDED.payment_method_id,
            payment_method_name = EXCLUDED.payment_method_name,
            raw = EXCLUDED.raw, fetched_at = EXCLUDED.fetched_at
    """, (
        row["qbo_payment_id"], row["qbo_customer_id"], row["type"],
        row["unapplied_amt"], row["total_amt"], row["txn_date"],
        row["ref_num"], row["memo"],
        row["payment_method_id"], row["payment_method_name"],
        psycopg2.extras.Json(row["raw"]), now,
    ))


def upsert_links_from_raw(cur, payment_id, raw, known_invoice_ids, txn_date):
    """Parse raw.Line[].LinkedTxn[] -> billing.payment_invoice_links.
    applied_via='external_qbo' because QBO already matched it; we're just
    mirroring the state. Skips invoices not in our billing.invoices table.

    applied_at is set to the payment's txn_date (when QBO records the
    payment as happening) rather than now() -- otherwise the "Applied"
    column in the UI reflects when OUR pull script first noticed the
    link, which can drift hundreds of days from reality. For manual /
    auto_match rows written elsewhere, applied_at = now() is correct
    because that IS the real event time.
    """
    written = 0
    lines = (raw or {}).get("Line") or []
    for line in lines:
        amount = line.get("Amount") or 0
        if amount <= 0:
            continue
        for lt in (line.get("LinkedTxn") or []):
            if lt.get("TxnType") != "Invoice":
                continue
            invoice_id = str(lt.get("TxnId") or "")
            if not invoice_id or invoice_id not in known_invoice_ids:
                continue
            cur.execute("""
                INSERT INTO billing.payment_invoice_links
                    (payment_id, invoice_id, amount, applied_via, applied_at)
                VALUES (%s, %s, %s, 'external_qbo', COALESCE(%s::timestamptz, now()))
                ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
                    amount = EXCLUDED.amount
                    -- Preserve applied_via + applied_at on updates: if we
                    -- originally tracked this as 'manual' or 'auto_match',
                    -- don't downgrade it to 'external_qbo' just because QBO
                    -- also sees it now.
            """, (payment_id, invoice_id, float(amount), txn_date))
            written += 1
    return written


def main(lookback_days: int = 180):
    print(f"=== pull_qbo_credits started (lookback={lookback_days} days) ===")
    access_token, realm_id = refresh_qbo_token()
    conn = get_db_conn()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Resolve PaymentMethod.id -> .name once per run
    print("Fetching PaymentMethod lookup...")
    pm_map = fetch_payment_method_map(access_token, realm_id)
    print(f"  {len(pm_map)} payment methods: {sorted(pm_map.values())}")

    # Load known invoice IDs so link-writes don't FK-fail on invoices we don't track
    cur = conn.cursor()
    cur.execute("SELECT qbo_invoice_id FROM billing.invoices")
    known_invoice_ids = {r[0] for r in cur.fetchall()}
    print(f"Known billing.invoices rows: {len(known_invoice_ids)}")
    cur.close()

    print(f"Fetching ALL payments since {cutoff}...")
    all_payments = qbo_query_all(
        f"SELECT * FROM Payment WHERE TxnDate >= '{cutoff}'",
        "Payment", access_token, realm_id,
    )
    print(f"  {len(all_payments)} payments")

    print("Fetching ALL credit memos...")
    all_credit_memos = qbo_query_all(
        "SELECT * FROM CreditMemo", "CreditMemo", access_token, realm_id,
    )
    print(f"  {len(all_credit_memos)} credit memos")

    rows = []
    for pmt in all_payments:
        pmref = pmt.get("PaymentMethodRef") or {}
        pm_id = pmref.get("value")
        rows.append({
            "qbo_payment_id": pmt.get("Id"),
            "qbo_customer_id": (pmt.get("CustomerRef") or {}).get("value"),
            "type": "payment",
            "unapplied_amt": float(pmt.get("UnappliedAmt") or 0),
            "total_amt": float(pmt.get("TotalAmt") or 0),
            "txn_date": pmt.get("TxnDate"),
            "ref_num": pmt.get("PaymentRefNum"),
            "memo": pmt.get("PrivateNote"),
            "payment_method_id": pm_id,
            "payment_method_name": pm_map.get(pm_id) if pm_id else None,
            "raw": pmt,
        })
    for cm in all_credit_memos:
        rows.append({
            "qbo_payment_id": f"CM-{cm.get('Id')}",
            "qbo_customer_id": (cm.get("CustomerRef") or {}).get("value"),
            "type": "credit_memo",
            "unapplied_amt": float(cm.get("RemainingCredit") or 0),
            "total_amt": float(cm.get("TotalAmt") or 0),
            "txn_date": cm.get("TxnDate"),
            "ref_num": cm.get("DocNumber"),
            "memo": cm.get("PrivateNote"),
            "payment_method_id": None,
            "payment_method_name": None,
            "raw": cm,
        })
    print(f"Total rows to upsert: {len(rows)}")

    cur = conn.cursor()
    upserted = 0
    links_written = 0
    for r in rows:
        upsert_payment(cur, r, now)
        upserted += 1
        links_written += upsert_links_from_raw(
            cur, r["qbo_payment_id"], r["raw"], known_invoice_ids,
            r.get("txn_date"),
        )
    conn.commit()

    cur.execute("""
        SELECT type,
               count(*) AS rows,
               count(*) FILTER (WHERE unapplied_amt > 0) AS open_rows,
               sum(unapplied_amt) AS total_unapplied,
               count(*) FILTER (WHERE was_charged) AS charged_rows
        FROM billing.customer_payments
        GROUP BY type
    """)
    by_type = {}
    for r in cur.fetchall():
        by_type[r[0]] = {
            "rows": r[1], "open_rows": r[2],
            "total_unapplied": float(r[3] or 0),
            "charged_rows": r[4],
        }
    cur.execute("SELECT count(*) FROM billing.payment_invoice_links")
    total_links = cur.fetchone()[0]
    cur.close()
    conn.close()

    total_unapplied = sum(t["total_unapplied"] for t in by_type.values())
    print(f"=== done: {upserted} payments upserted, {links_written} links written ===")
    print(f"  by type: {by_type}")
    print(f"  total_invoice_links in DB: {total_links}")
    print(f"  total unapplied: ${total_unapplied:,.2f}")

    return {
        "status": "success",
        "upserted": upserted,
        "links_written_this_run": links_written,
        "total_links_in_db": total_links,
        "by_type": by_type,
        "total_unapplied": total_unapplied,
        "qbo_payments_scanned": len(all_payments),
        "qbo_credit_memos_scanned": len(all_credit_memos),
        "payment_methods_resolved": len(pm_map),
    }
