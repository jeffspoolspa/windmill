import requests
import wmill
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]), timeout=30)
    if not resp.ok: raise Exception(f"Token refresh failed: {resp.status_code}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"], resource["realm_id"]

def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"))

def qbo_query_all(query, entity, access_token, realm_id):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    results = []; start = 1
    while True:
        resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers, params={"query": f"{query} STARTPOSITION {start} MAXRESULTS 1000"}, timeout=60)
        if not resp.ok: break
        batch = resp.json().get("QueryResponse", {}).get(entity, [])
        results.extend(batch)
        print(f"  page {(start-1)//1000+1}: {len(batch)} (total {len(results)})")
        if len(batch) < 1000: break
        start += 1000
    return results

def main():
    print("=== FULL CREDIT PULL (no date filter) ===")
    access_token, realm_id = refresh_qbo_token()
    conn = get_db_conn()
    now = datetime.now(timezone.utc)

    print("Fetching ALL payments...")
    all_payments = qbo_query_all("SELECT * FROM Payment", "Payment", access_token, realm_id)
    unapplied = [p for p in all_payments if float(p.get("UnappliedAmt", 0)) > 0]
    print(f"  {len(all_payments)} total, {len(unapplied)} unapplied")

    print("Fetching ALL credit memos...")
    all_cms = qbo_query_all("SELECT * FROM CreditMemo", "CreditMemo", access_token, realm_id)
    unapplied_cms = [c for c in all_cms if float(c.get("RemainingCredit", 0)) > 0]
    print(f"  {len(all_cms)} total, {len(unapplied_cms)} unapplied")

    credits = []
    for p in unapplied:
        credits.append({"qbo_payment_id": p["Id"], "qbo_customer_id": p.get("CustomerRef",{}).get("value"),
            "type": "payment", "unapplied_amt": float(p.get("UnappliedAmt",0)),
            "total_amt": float(p.get("TotalAmt",0)), "txn_date": p.get("TxnDate"),
            "ref_num": p.get("PaymentRefNum"), "memo": p.get("PrivateNote"), "raw": p})
    for c in unapplied_cms:
        credits.append({"qbo_payment_id": f"CM-{c['Id']}", "qbo_customer_id": c.get("CustomerRef",{}).get("value"),
            "type": "credit_memo", "unapplied_amt": float(c.get("RemainingCredit",0)),
            "total_amt": float(c.get("TotalAmt",0)), "txn_date": c.get("TxnDate"),
            "ref_num": c.get("DocNumber"), "memo": c.get("PrivateNote"), "raw": c})

    cur = conn.cursor()
    upserted = 0
    for c in credits:
        cur.execute("""INSERT INTO billing.open_credits
            (qbo_payment_id, qbo_customer_id, type, unapplied_amt, total_amt, txn_date, ref_num, memo, raw, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (qbo_payment_id) DO UPDATE SET
            unapplied_amt=EXCLUDED.unapplied_amt, total_amt=EXCLUDED.total_amt,
            txn_date=EXCLUDED.txn_date, ref_num=EXCLUDED.ref_num, memo=EXCLUDED.memo,
            raw=EXCLUDED.raw, fetched_at=EXCLUDED.fetched_at""",
            (c["qbo_payment_id"], c["qbo_customer_id"], c["type"], c["unapplied_amt"],
             c["total_amt"], c["txn_date"], c["ref_num"], c["memo"],
             psycopg2.extras.Json(c["raw"]), now))
        upserted += 1
    conn.commit()

    live_ids = [c["qbo_payment_id"] for c in credits]
    if live_ids:
        cur.execute("DELETE FROM billing.open_credits WHERE qbo_payment_id != ALL(%s)", (live_ids,))
    removed = cur.rowcount
    conn.commit()

    # Re-evaluate: reset ready_to_process WOs whose customer now has unmatched credits
    cur.execute("""
        UPDATE public.work_orders w
        SET billing_status = 'awaiting_invoice', billing_status_set_at = now()
        WHERE w.billing_status = 'ready_to_process'
          AND EXISTS (
            SELECT 1 FROM billing.open_credits oc
            JOIN billing.invoices i ON i.qbo_customer_id = oc.qbo_customer_id
            WHERE i.doc_number = w.invoice_number
              AND oc.unapplied_amt > 0
              AND oc.matched_wo_number IS NULL
              AND COALESCE(oc.memo, '') NOT ILIKE '%%maint%%'
          )
    """)
    reset_count = cur.rowcount
    conn.commit()

    # Re-trigger classification for reset WOs
    reclassified = 0
    if reset_count > 0:
        cur.execute("""
            UPDATE public.work_orders SET invoice_number = invoice_number
            WHERE billing_status = 'awaiting_invoice'
              AND invoice_number IN (SELECT doc_number FROM billing.invoices)
        """)
        reclassified = cur.rowcount
        conn.commit()
        print(f"Reset {reset_count} WOs, reclassified {reclassified}")

    cur.execute("SELECT type, count(*), sum(unapplied_amt) FROM billing.open_credits GROUP BY type")
    by_type = {r[0]: {"count": r[1], "total": float(r[2])} for r in cur.fetchall()}

    cur.execute("SELECT billing_status, count(*) FROM public.work_orders WHERE billing_status != 'not_billable' GROUP BY billing_status ORDER BY count(*) DESC")
    final_state = {r[0]: r[1] for r in cur.fetchall()}

    cur.close(); conn.close()
    total = sum(t["total"] for t in by_type.values())
    print(f"=== done: {upserted} credits, {reset_count} WOs re-evaluated, ${total:,.2f} total ===")

    return {"upserted": upserted, "removed": removed, "by_type": by_type,
            "total_unapplied": total, "wos_reset": reset_count, "wos_reclassified": reclassified,
            "final_state": final_state,
            "payments_scanned": len(all_payments), "payments_unapplied": len(unapplied),
            "cms_unapplied": len(unapplied_cms)}
