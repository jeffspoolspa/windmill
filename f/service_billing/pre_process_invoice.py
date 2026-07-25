# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/pre_process_invoice — enrich one invoice.
#
# Settle the credits, decide the payment method, derive the class and the
# memo, push both to QBO, record what we decided. That is the whole job.
#
# NOT here, on purpose: readiness (billing.invoice_ready), needs_review vs the
# processing queue (projection triggers), events (emitted by the primitives),
# retries (the queue's attempts ledger). This script states facts; the
# database decides what they mean.

from f.billing._lib.db import get_db_conn
from f.billing._lib.clients import QboClient
from f.billing._lib import calc, credits, customers, invoices
from f.service_billing.memo import resolve_memo


def enrich(conn, qbo, qbo_invoice_id):
    """The job. Separate from main() only because Windmill builds the script's
    input form from main's signature — the dispatcher needs to inject its own
    connection and client (one token refresh per drain, not per invoice)."""
    invoice = invoices.load(conn, qbo_invoice_id)
    qbo_invoice = qbo.get_invoice(qbo_invoice_id)          # read = echo
    if not qbo_invoice:
        raise RuntimeError(f"QBO invoice {qbo_invoice_id} unreadable")

    # credits: apply the ones that clearly belong here, against what is
    # still owed. A credit that doesn't match is left open — it surfaces
    # as undecided in invoice_ready for a human.
    balance = float(qbo_invoice.get("Balance") or 0)
    applied = 0.0
    for credit in credits.open_for(conn, invoice["qbo_customer_id"]):
        if balance <= 0:
            break
        reason = calc.credit_match_reason(credit, invoice["wo_number"], balance)
        if reason:
            amount = credits.apply(conn, qbo, credit, invoice, reason)
            applied, balance = applied + amount, round(balance - amount, 2)

    # Converge the balance we just changed. apply_credits fresh-reads BEFORE
    # applying, so our cached balance is stale-high afterwards — and
    # billing.invoice_ready reads it to decide whether the remaining open
    # credits still matter. One read, only when we actually moved money.
    if applied:
        qbo.get_invoice(qbo_invoice_id)                        # read = echo

    route = customers.payment_route(conn, qbo, invoice["qbo_customer_id"],
                                    invoice["wo_text"])
    qbo_class = calc.derive_qbo_class(invoice["assigned_to"], invoice["wo_type"],
                                      invoice["work_description"])
    memo = resolve_memo(invoice, invoice)
    composed = calc.compose_memo(invoice["wo_number"], memo["text"],
                                 memo["source"] == "locked")

    # QBO first: it echoes + emits invoice_edited itself. If it refuses, we
    # have not enriched anything — raise, and let the queue's attempts ledger
    # retry and eventually dead-letter. Never record an enrichment QBO
    # didn't accept.
    if composed:
        patch = qbo.update_invoice(qbo_invoice_id,
                                   calc.enrichment_updates(composed, qbo_class,
                                                           qbo.class_id(qbo_class),
                                                           invoice["completed"],
                                                           qbo_invoice.get("TxnDate")),
                                   intent_ref="pre_process")
        if not patch.get("success"):
            raise RuntimeError(f"QBO rejected the enrichment patch: "
                               f"{patch.get('error')}")

    # Our half of the row: the route, and the fact that we ran. QBO knows
    # nothing about any of it — no echo can produce these. (memo/qbo_class
    # ride along because the gate and the UI read columns, not raw jsonb.)
    invoices.write_enrichment(conn, qbo_invoice_id, **route,
                              qbo_class=qbo_class, memo=composed,
                              memo_locked=memo["locked"],
                              statement_memo=memo.get("statement") or composed)
    return {"qbo_invoice_id": qbo_invoice_id, "wo_number": invoice["wo_number"],
            "memo": composed, "qbo_class": qbo_class,
            "credits_applied": applied,
            "payment_method_id": route["target_payment_method_id"]}


def main(qbo_invoice_id: str = None):
    """One invoice, on its own connection. Batches enqueue into
    billing.service_preprocess_queue; the dispatcher drains it."""
    if not qbo_invoice_id:
        return {"error": "qbo_invoice_id required (batches go through the queue)"}
    conn = get_db_conn()
    try:
        return enrich(conn, QboClient(conn), qbo_invoice_id)
    finally:
        conn.close()
