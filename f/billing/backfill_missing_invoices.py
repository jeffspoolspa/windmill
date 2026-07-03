# backfill_missing_invoices — pull cache-missing QBO invoices by DocNumber
#
# Purpose:
#   The CDC poll TRUNCATES when a window has too many changes (QBO caps CDC
#   responses per entity) and the cursor can jump past dropped rows — during
#   a month-end ION batch sync some invoices land in QBO but never reach
#   billing.invoices, so their billing periods sit unlinked forever. This
#   script finds stamped-but-missing ION invoice numbers for a month, asks
#   QBO for them by DocNumber, and runs each through refresh_invoice's
#   canonical upsert (which fires the link trigger -> preprocess queue).
#
# Tables:
#   billing_audit.task_billing_periods  [read]  stamped ion_invoice_number
#                                               with no cache row
#   billing.invoices                    [write] via refresh_invoice upsert
#
# External APIs:
#   - QBO query API (read): SELECT Id FROM Invoice WHERE DocNumber IN (...)
#
# Idempotent: re-running only touches doc numbers still missing from the
# cache; invoices genuinely not in QBO (unsynced/on-hold in ION) are
# reported as not_in_qbo and skipped.

import psycopg2
import psycopg2.extras
import requests
import wmill

from f.service_billing.refresh_invoice import main as refresh_invoice
from f.service_billing.refresh_invoice import refresh_qbo_token

SUPABASE_RESOURCE = "u/carter/supabase"
CHUNK = 25  # DocNumber IN (...) chunk size — keeps the query URL sane


def get_db_conn():
    cfg = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=cfg["host"], port=cfg.get("port", 5432), dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def qbo_query(sql, access_token, realm_id):
    resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": sql},
        timeout=60,
    )
    if not resp.ok:
        raise Exception(f"QBO query failed: {resp.status_code} {resp.text[:200]}")
    return resp.json().get("QueryResponse", {})


def main(billing_month: str = "", dry_run: bool = False):
    """billing_month: 'YYYY-MM' (required). dry_run: report only."""
    if not billing_month:
        return {"status": "error", "error": "billing_month (YYYY-MM) required"}

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT DISTINCT tbp.ion_invoice_number AS doc
           FROM billing_audit.task_billing_periods tbp
           WHERE tbp.billing_month = %s
             AND tbp.ion_invoice_number IS NOT NULL
             AND tbp.qbo_invoice_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM billing.invoices i
                             WHERE i.doc_number = tbp.ion_invoice_number)
           ORDER BY 1""",
        (f"{billing_month}-01",))
    missing = [r["doc"] for r in cur.fetchall()]
    conn.close()
    print(f"{len(missing)} stamped doc numbers missing from the cache")
    if not missing:
        return {"status": "noop", "missing": 0}

    access_token, realm_id = refresh_qbo_token()
    found = {}  # doc -> qbo Id
    for i in range(0, len(missing), CHUNK):
        chunk = missing[i:i + CHUNK]
        in_list = ",".join(f"'{d}'" for d in chunk)
        rows = qbo_query(
            f"SELECT Id, DocNumber FROM Invoice WHERE DocNumber IN ({in_list})",
            access_token, realm_id).get("Invoice", [])
        for r in rows:
            found[r["DocNumber"]] = r["Id"]

    not_in_qbo = [d for d in missing if d not in found]
    print(f"{len(found)} found in QBO, {len(not_in_qbo)} not in QBO (unsynced in ION)")

    results = []
    if not dry_run:
        for doc, qbo_id in found.items():
            try:
                out = refresh_invoice(qbo_id)
                results.append({"doc": doc, "qbo_id": qbo_id,
                                "status": out.get("status", "ok")})
                print(f"  refreshed #{doc} (id {qbo_id}): {out.get('status')}")
            except Exception as e:
                results.append({"doc": doc, "qbo_id": qbo_id, "status": "error",
                                "error": str(e)[:200]})
                print(f"  ERROR #{doc}: {e}")

    return {"status": "ok", "missing": len(missing), "found_in_qbo": len(found),
            "not_in_qbo": not_in_qbo, "refreshed": results, "dry_run": dry_run}
