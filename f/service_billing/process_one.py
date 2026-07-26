# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/process_one — process ONE service-billing invoice.
#
# The goal is processed = settled AND delivered. This script takes the single
# step that moves the invoice toward it. It checks NO state: readiness is
# enforced ATOMICALLY in the queue worker's CLAIM (AND invoice_ready(...)),
# and state changes dequeue waiting rows via trigger — the queue is the only
# door. Charge history, instrument, labels, receipts = charge_and_record;
# attempt surfacing = the attempts_ok indicator. force = the human override
# that runs outside the queue.

from f.billing._lib.db import get_db_conn, query_one
from f.billing._lib.qbo import set_rate_limiter, refresh_qbo_token, fetch_qbo_invoice
from f.billing._lib.payments import charge_and_record
from f.billing._lib.delivery import send_and_record

STAGE = "process"

# charge outcome -> what the engine does next. (Shrinks to ~4 rows when the
# service's status enum collapses — the one remaining interface diet.)
CHARGE_NEXT = {
    "succeeded":         "send",              # deliver the paid copy
    "already_paid":      "send",
    "already_succeeded": "already_succeeded",
    "uncertain":         "uncertain",         # reconciler owns it
    "read_failed":       "error",
    "declined":          "needs_human",       # all four surface via attempts_ok
    "declined_no_retry": "needs_human",
    "payment_orphan":    "needs_human",
    "blocked_reconcile": "needs_human",
    "no_payment_method": "needs_human",
}


def step(settled, delivered, route):
    if settled and delivered:
        return "done"
    if not settled and route != "email":
        return "charge"
    return "send"


def process_one(conn, qbo_invoice_id, access_token, realm_id, force=False):
    result = {"qbo_invoice_id": qbo_invoice_id}

    # our row first (route, target, CACHED balance, WO) — then the leader's
    invoice = query_one(conn, """SELECT i.*, w.wo_number
                        FROM billing.invoices i
                        JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
                        WHERE i.qbo_invoice_id = %s""", (qbo_invoice_id,))
    qbo_invoice, fetch_error = fetch_qbo_invoice(qbo_invoice_id, access_token,
                                                 realm_id, conn=conn)  # read = echo
    if not qbo_invoice:
        return {**result, "status": "error", "error": f"qbo_fetch_failed: {fetch_error}"}
    balance = float(qbo_invoice.get("Balance", 0) or 0)

    # MIRROR CHECK (gate 6 at the money moment): the gates were judged on OUR
    # picture — a differing QBO balance means something landed we haven't
    # accounted for. Pause: converge the cache, return "retry"; the worker
    # re-opens the claim and the atomic gate re-asks readiness on the NEW
    # state (a just-landed credit holds the invoice until decided).
    cached_balance = float(invoice.get("balance") or 0)
    if abs(cached_balance - balance) > 0.01:
        return {**result, "status": "retry",
                "reason": f"balance drift (cache {cached_balance} vs QBO {balance}) — resynced"}
    route = invoice["preferred_payment_type"]

    action = step(balance <= 0, qbo_invoice.get("EmailStatus") == "EmailSent", route)
    charge_result = None
    if action == "charge":
        charge_result = charge_and_record(conn, {
            "stage": STAGE, "qbo_invoice_id": qbo_invoice_id,
            "preferred_type": route, "force_retry": force,
            "target_payment_method_id": invoice["target_payment_method_id"],
        }, access_token, realm_id)
        # a successful charge FALLS THROUGH to the send below (CHARGE_NEXT
        # maps succeeded -> "send": the customer gets the paid copy)
        action = CHARGE_NEXT.get(charge_result["status"], "error")
        result["charge"] = {key: charge_result.get(key) for key in
                            ("status", "amount", "charge_id", "payment_id",
                             "attempt_id", "receipt_sent", "error")}

    if action == "send":  # reached directly (email route) OR after the charge
        send_result = result["send"] = send_and_record(conn, invoice, balance, STAGE,
                                                       access_token, realm_id)
        return {**result,
                "status": "succeeded" if send_result["success"] else "needs_human",
                **({} if send_result["success"] else {"reason": "email_failed"})}

    return {**result, "status": action if action != "done" else "already_succeeded",
            **({"reason": charge_result.get("error")}
               if charge_result and action == "needs_human" else {}),
            **({"error": charge_result.get("error")}
               if charge_result and action == "error" else {})}


def main(qbo_invoice_id: str, force: bool = False):
    """FORCE-ONLY direct run (human override, outside the queue). Normal
    processing always goes through the queue: f/service_billing/process_invoice."""
    if not qbo_invoice_id:
        return {"status": "error", "error": "qbo_invoice_id required"}
    if not force:
        return {"status": "error",
                "error": "normal runs go through the queue (process_invoice); "
                         "this direct entry is force-only"}
    conn = get_db_conn()
    set_rate_limiter(conn)
    try:
        access_token, realm_id = refresh_qbo_token()
        return process_one(conn, qbo_invoice_id, access_token, realm_id, force=True)
    finally:
        conn.close()
