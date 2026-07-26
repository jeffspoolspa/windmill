# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/recover_payment — explicit human recovery for a
# payment_orphan (charge captured at Intuit, QBO Payment record failed).
# Retries ONLY the ledger write with the attempt's persisted charge — it can
# NEVER charge again (f/billing/_lib/payments.recover_orphan refuses anything
# else). Separate script on purpose: recovery is not processing, so the
# processing engine carries no recovery mode.

from f.billing._lib.db import get_db_conn, query_one
from f.billing._lib.qbo import (
    set_rate_limiter, refresh_qbo_token, send_payment_receipt, fetch_qbo_invoice,
)
from f.billing._lib.payments import recover_orphan
from f.billing._lib.delivery import send_and_record
from f.billing._lib.events import emit


def main(qbo_invoice_id: str):
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}
    conn = get_db_conn()
    set_rate_limiter(conn)
    try:
        access_token, realm_id = refresh_qbo_token()
        invoice_row = query_one(conn, """SELECT i.*, w.wo_number
                             FROM billing.invoices i
                             JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
                             WHERE i.qbo_invoice_id = %s""", (qbo_invoice_id,))
        if not invoice_row:
            return {"status": "error", "error": "invoice not cached / WO not linked"}
        recovery = recover_orphan(conn, qbo_invoice_id, "process", invoice_row["qbo_customer_id"],
                           invoice_row["wo_number"],
                           f"Auto-charge | WO# {invoice_row['wo_number']} | Inv# {invoice_row['doc_number']}",
                           access_token, realm_id)
        if recovery["status"] != "recovered":
            return {"status": "error", **recovery}
        receipt = send_payment_receipt(recovery["payment_id"], invoice_row["qbo_customer_id"],
                                       access_token, realm_id)
        if receipt.get("success"):
            emit(conn, "payment", recovery["payment_id"], "receipt_sent",
                 participants=[f"customer:{invoice_row['qbo_customer_id']}"],
                 payload={"provenance": {"source": "intent",
                                         "intent_ref": recovery.get("attempt_id")}})
        # the paid copy goes through the ONE send path (WAL + invoice_emailed)
        copy = send_and_record(conn, invoice_row, 0, "process", access_token, realm_id)
        fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id, conn=conn)
        return {"status": "succeeded", "recovered_from": "payment_orphan", **recovery,
                "receipt_sent": receipt.get("success"), "invoice_email": copy}
    finally:
        conn.close()
