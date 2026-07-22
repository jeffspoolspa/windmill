# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/process_invoice — the service-billing event handler.
#
# Charges cards / sends invoices for invoices whose DERIVED status is
# ready_to_process (billing.v_invoice_status — the engine records state;
# the view computes status: processed = paid AND sent, sent+open = open_ar).
# Every external verb imports from f/billing/_lib; the idempotent charge core
# (WAL + fresh-read + charge + QBO Payment + receipt) is charge_and_record.
# This file keeps only THIS workflow's policy: route, pre-flight gates,
# credit halts, delivery, status stamps (derived in Phase 3).
# WAL states + resume rules: f/billing/_lib/payments docstring + ADR 009.

import time
import psycopg2.extras

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import (
    set_rate_limiter, refresh_qbo_token, fetch_qbo_invoice, fetch_qbo_customer_email,
    send_invoice_email, send_payment_receipt, bump_invoice_due_date_to_today,
    record_qbo_payment,
)
from f.billing._lib.wal import (
    latest_attempt, create_attempt, update_attempt,
    insert_webhook_expectation, dumps as _dumps,
)
from f.billing._lib.payments import (
    charge_and_record, resolve_payment_method, load_applicable_credits,
)
from f.billing._lib.cache import echo_invoice

STAGE = "process"
EMAIL_RETRY_MAX = 3
EMAIL_RETRY_BACKOFF_S = 5


# ── engine reads + status writes ─────────────────────────────────────────────

def _row(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def load_invoice(conn, qbo_invoice_id):
    # derived_status comes from billing.v_invoice_status — the ONE home for
    # the status rules (processed = paid AND sent; sent+open = open_ar).
    return _row(conn, """SELECT i.*, s.derived_status
                         FROM billing.invoices i
                         JOIN billing.v_invoice_status s USING (qbo_invoice_id)
                         WHERE i.qbo_invoice_id = %s""", (qbo_invoice_id,))


def load_linked_wo(conn, qbo_invoice_id):
    return _row(conn, "SELECT * FROM public.work_orders WHERE qbo_invoice_id = %s LIMIT 1",
                (qbo_invoice_id,))


def mark_invoice_needs_review(conn, qbo_invoice_id, reason):
    # Never demotes 'processed' (that would unwind a successful payment).
    _exec(conn, """UPDATE billing.invoices
                   SET billing_status = 'needs_review', needs_review_reason = %s
                   WHERE qbo_invoice_id = %s AND billing_status != 'processed'""",
          (reason, qbo_invoice_id))


def _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id):
    fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if fresh:
        echo_invoice(conn, qbo_invoice_id, fresh)


# ── the sentences ────────────────────────────────────────────────────────────

def _result(qbo_invoice_id, status, **rest):
    return {"qbo_invoice_id": qbo_invoice_id, "status": status, **rest}


def _route(invoice):
    """Charge vs email, from preferred_payment_type; legacy payment_method
    fallback for never-re-pre-processed invoices; None = unusable."""
    preferred = invoice.get("preferred_payment_type")
    if preferred in ("email", "ach", "credit_card"):
        return preferred
    return {"invoice": "email", "on_file": "credit_card"}.get(invoice.get("payment_method"))


def build_intent(invoice, wo_number, pm, receipt_email):
    """Policy as data (ADR 009 §B): the service reads the balance fresh — no
    amount is passed. receipt_email None = no receipt."""
    invoice_number = invoice.get("doc_number")
    return {
        "stage": STAGE,
        "qbo_invoice_id": invoice["qbo_invoice_id"],
        "lines": [invoice["qbo_invoice_id"]],
        "payment_method_id": pm["method_id"],
        "cpm_id": pm["cpm_id"],
        "channel": "card" if pm["payment_type"] in ("credit_card", "card") else "ach",
        "customer_id": invoice.get("qbo_customer_id"),
        "customer_name": invoice.get("customer_name") or "",
        "invoice_number": invoice_number,
        "wo_number": wo_number,
        "payment_method": invoice.get("payment_method"),  # legacy dual-write
        "payment_ref": wo_number,
        "memo_prefix": f"Auto-charge | WO# {wo_number} | Inv# {invoice_number}",
        "receipt_email": receipt_email,
    }


def deliver(conn, qbo_invoice_id, customer_id, access_token, realm_id):
    """The customer gets a paid invoice copy alongside the receipt.
    send_invoice_email is idempotent (EmailSent -> skip)."""
    inv_email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
    if inv_email.get("success") and not inv_email.get("skipped"):
        # QBO fires Invoice.Emailed only on an actual send.
        insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
    return inv_email


def process_one(conn, qbo_invoice_id, access_token, realm_id,
                dry_run=False, recover_orphan=False, force=False):
    """Main per-invoice flow. Returns dict with status + diagnostics."""
    invoice = load_invoice(conn, qbo_invoice_id)
    if not invoice:
        return _result(qbo_invoice_id, "error", error="invoice not found in billing.invoices")
    wo = load_linked_wo(conn, qbo_invoice_id)
    if not wo:
        return _result(qbo_invoice_id, "error", error="no linked work order — cannot process")
    wo_number = wo["wo_number"]
    customer_id = invoice.get("qbo_customer_id")

    def halt(reason_key, review_reason, attempt, **rest):
        mark_invoice_needs_review(conn, qbo_invoice_id, review_reason)
        return _result(qbo_invoice_id, "needs_human", reason=reason_key,
                       attempt_id=str(attempt["id"]), **rest)

    route = _route(invoice)
    if route is None:
        err = (f"invalid preferred_payment_type "
               f"'{invoice.get('preferred_payment_type')}' and no legacy "
               f"payment_method to fall back to (re-run pre_process_invoice)")
        # Stub attempt so the batch progress modal sees this invoice was touched.
        attempt = create_attempt(conn, qbo_invoice_id, STAGE, invoice.get("doc_number"),
                                 "email", 0, dry_run, wo_number=wo_number, status="error")
        update_attempt(conn, attempt["id"], error_message=err[:500])
        mark_invoice_needs_review(conn, qbo_invoice_id, err[:200])
        return _result(qbo_invoice_id, "error", attempt_id=str(attempt["id"]), error=err)

    if invoice.get("derived_status") != "ready_to_process" and not (force or recover_orphan):
        return _result(qbo_invoice_id, "skipped",
                       reason=f"status='{invoice.get('derived_status')}' "
                              f"(need ready_to_process or force=True)")

    # CLAIM-TIME READINESS GUARD (derived readiness v3): the gate and the
    # guard are the SAME code — billing.invoice_ready(), the one rule list.
    # The charge is the irreversible act, so readiness is re-verified here at
    # claim regardless of what enqueued us (queue row = invitation, not
    # command). If the block is undecided credits, re-enqueue pre-process so
    # the matcher proposes on the new credit through the normal path.
    if not (force or recover_orphan):
        chk = _row(conn, """
            SELECT ready, undecided_credit_count
            FROM billing.v_service_billing_state
            WHERE qbo_invoice_id = %s
        """, (qbo_invoice_id,))
        if not chk or not chk["ready"]:
            undecided = chk["undecided_credit_count"] if chk else 0
            if undecided and undecided > 0:
                _exec(conn, """INSERT INTO billing.service_preprocess_queue (qbo_invoice_id)
                               VALUES (%s)
                               ON CONFLICT (qbo_invoice_id) WHERE finished_at IS NULL
                               DO NOTHING""", (qbo_invoice_id,))
            return _result(qbo_invoice_id, "skipped",
                           reason=("invoice_ready() = false at claim time"
                                   + (f" — {undecided} undecided credit(s), re-enqueued "
                                      f"pre-process" if undecided else "")))

    # PRE-FLIGHT: prior-attempt policy gates. (payment_orphan needs no gate
    # here — charge_and_record refuses orphans and the dispatch handles it.)
    prior = latest_attempt(conn, qbo_invoice_id, STAGE)

    if recover_orphan:  # explicit human action
        if not prior or prior["status"] != "payment_orphan":
            return _result(qbo_invoice_id, "error",
                           error=f"recover_orphan called but no payment_orphan attempt found "
                                 f"(prior status: {prior['status'] if prior else 'none'})")
        return _recover_orphan(conn, prior, invoice, wo_number, access_token, realm_id)

    # Prior succeeded: done unless force wants the remaining open balance
    # (the "charge balance" recovery flow on the WO page).
    if prior and prior["status"] == "succeeded":
        qbo_inv_chk, _err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if qbo_inv_chk:
            echo_invoice(conn, qbo_invoice_id, qbo_inv_chk)
            chk_balance = float(qbo_inv_chk.get("Balance", 0) or 0)
            if chk_balance == 0:
                return _result(qbo_invoice_id, "already_succeeded", attempt_id=str(prior["id"]))
            if not force:
                return _result(qbo_invoice_id, "already_succeeded", attempt_id=str(prior["id"]),
                               note=f"prior succeeded; open balance ${chk_balance:.2f} "
                                    f"— pass force=true to create a recovery attempt")
            # force: fall through — the service fresh-reads and charges the remainder
        elif not force:
            return _result(qbo_invoice_id, "already_succeeded", attempt_id=str(prior["id"]),
                           note="prior succeeded but QBO state could not be verified")

    # A REAL prior decline (charge_id set — Intuit assigns one even on decline)
    # only blocks a retry of the SAME (channel, PM) path. Pre-charge halts
    # (credits/no-PM) have charge_id NULL and retry freely.
    if (prior and prior["status"] == "charge_declined" and not force
            and prior.get("charge_id") and route != "email"
            and prior.get("channel") == route
            and (str(prior["customer_payment_method_id"])
                 if prior.get("customer_payment_method_id") else None)
            == (str(invoice["target_payment_method_id"])
                if invoice.get("target_payment_method_id") else None)):
        return halt("charge_declined",
                    f"charge_declined ({(prior.get('error_message') or 'declined')[:120]})",
                    prior, error=prior.get("error_message"),
                    note="prior attempt declined this same PM; "
                         "change channel/PM or pass force=true to retry")

    if prior and prior["status"] == "needs_reconcile_review" and not force:
        return halt("needs_reconcile_review",
                    f"needs_reconcile_review ({(prior.get('error_message') or 'reconciler could not determine state')[:120]})",
                    prior, error=prior.get("error_message"))

    # Refresh QBO state — may have been paid/sent externally.
    qbo_inv, err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if not qbo_inv:
        return _result(qbo_invoice_id, "error", error=f"qbo_fetch_failed: {err}")
    echo_invoice(conn, qbo_invoice_id, qbo_inv)
    qbo_balance = float(qbo_inv.get("Balance", 0) or 0)
    qbo_email_sent = qbo_inv.get("EmailStatus") == "EmailSent"
    if qbo_balance == 0 and qbo_email_sent:
        return _result(qbo_invoice_id, "already_paid_and_sent")

    # DRY-RUN: sandbox plan on its OWN dry_run=true row — never touches live WAL.
    if dry_run:
        target = invoice.get("target_payment_method_id")
        attempt = create_attempt(conn, qbo_invoice_id, STAGE, invoice.get("doc_number"),
                                 route if target else "email", qbo_balance, True,
                                 wo_number=wo_number,
                                 payment_method=invoice.get("payment_method"),
                                 cpm_id=str(target) if target else None)
        plan = _build_dry_run_plan(conn, invoice, route, qbo_balance, qbo_email_sent,
                                   customer_id, attempt)
        update_attempt(conn, attempt["id"], status="succeeded", raw_result=_dumps(plan))
        return _result(qbo_invoice_id, "dry_run_complete",
                       attempt_id=str(attempt["id"]), plan=plan)

    if route == "email":
        return _process_email_only(conn, prior, invoice, wo_number, customer_id,
                                   access_token, realm_id)
    return _process_charge_path(conn, prior, invoice, wo_number, route,
                                qbo_balance, halt, access_token, realm_id)


def _build_dry_run_plan(conn, invoice, route, balance, email_already_sent,
                        customer_id, attempt):
    """Predicts a live run without external calls — mirrors the live halts."""
    is_charge = route in ("ach", "credit_card")
    plan = {
        "payment_method": invoice.get("payment_method"),
        "preferred_payment_type": route,
        "amount_to_charge": balance if is_charge and balance > 0 else 0,
        "would_send_invoice_email": not email_already_sent,
        "would_send_receipt": is_charge and balance > 0,
        "idempotency_key": attempt["idempotency_key"],
    }
    if is_charge and balance > 0:
        credits = load_applicable_credits(conn, customer_id)
        if credits:
            plan["would_halt"] = "credits_available"
            plan["credits_found"] = [
                {"qbo_payment_id": c.get("qbo_payment_id"),
                 "unapplied_amt": float(c.get("unapplied_amt") or 0),
                 "txn_date": str(c.get("txn_date")) if c.get("txn_date") else None,
                 "memo": c.get("memo")} for c in credits]
            plan["credits_total_unapplied"] = sum(
                float(c.get("unapplied_amt") or 0) for c in credits)
        target = invoice.get("target_payment_method_id")
        pm = resolve_payment_method(conn, customer_id, preferred_type=route,
                                    cpm_id=str(target) if target else None)
        plan["payment_method_on_file"] = pm
        if not pm.get("has_method"):
            plan["would_fail"] = pm.get("error") or "no_payment_method"
    return plan


def _halt_attempt(conn, prior, invoice, wo_number, channel, amount, reason, extra=None):
    """Pre-charge halt on the WAL (charge_declined + charge_id NULL = never
    reached Intuit). Reuses a prior pending row instead of stranding it."""
    attempt = prior if (prior and prior["status"] == "pending") else create_attempt(
        conn, invoice["qbo_invoice_id"], STAGE, invoice.get("doc_number"), channel,
        amount, False, wo_number=wo_number, payment_method=invoice.get("payment_method"))
    update_attempt(conn, attempt["id"], status="charge_declined", error_message=reason,
                   charge_result=_dumps(extra) if extra else None)
    return attempt


def _send_only(conn, prior, invoice, wo_number, channel, balance, note,
               access_token, realm_id):
    """No-charge outcome (zero balance / paid upstream): invoice copy only."""
    qbo_invoice_id = invoice["qbo_invoice_id"]
    email = send_invoice_email(qbo_invoice_id, invoice.get("qbo_customer_id"),
                               access_token, realm_id)
    attempt = prior if (prior and prior["status"] == "pending") else create_attempt(
        conn, qbo_invoice_id, STAGE, invoice.get("doc_number"), channel, balance,
        False, wo_number=wo_number, payment_method=invoice.get("payment_method"))
    payload = _dumps({"email": email, "skipped_charge_zero_balance": True})
    if not email["success"] and not email.get("skipped"):
        update_attempt(conn, attempt["id"], status="email_failed", email_sent=False,
                       error_message=email.get("error"), raw_result=payload)
        return _result(qbo_invoice_id, "email_failed",
                       attempt_id=str(attempt["id"]), error=email.get("error"))
    update_attempt(conn, attempt["id"], status="succeeded",
                   email_sent=email["success"], raw_result=payload)
    return _result(qbo_invoice_id, "succeeded", attempt_id=str(attempt["id"]), note=note)


def _process_charge_path(conn, prior, invoice, wo_number, route, balance, halt,
                         access_token, realm_id):
    qbo_invoice_id = invoice["qbo_invoice_id"]
    customer_id = invoice.get("qbo_customer_id")

    if balance == 0:  # covered by credits in pre_process — no charge needed
        return _send_only(conn, prior, invoice, wo_number, "email", 0,
                          "balance was zero — sent invoice only", access_token, realm_id)

    # Credit re-check: credits that landed since pre_process halt the charge.
    # Credits visible at review time (txn_date on/before the override stamp)
    # are waved through; NEWER credits still halt.
    credits = load_applicable_credits(conn, customer_id)
    override_at = invoice.get("credit_review_overridden_at")
    if credits and override_at is not None:
        cutoff = override_at.date() if hasattr(override_at, "date") else override_at
        credits = [c for c in credits
                   if c.get("txn_date") is None or c.get("txn_date") > cutoff]
    if credits:
        total_unapplied = sum(float(c.get("unapplied_amt") or 0) for c in credits)
        reason = f"credits_available ({len(credits)} credit(s), ${total_unapplied:.2f} unapplied)"
        attempt = _halt_attempt(conn, prior, invoice, wo_number, "email", balance,
                                reason, extra={"credits_found": credits})
        return halt("credits_available", reason, attempt, error=reason,
                    credits_found=len(credits), total_unapplied=total_unapplied)

    # The PM pre_process picked (stable across the UI session); legacy
    # invoices without a target fall back to the default picker.
    target = invoice.get("target_payment_method_id")
    pm = resolve_payment_method(conn, customer_id,
                                preferred_type=invoice.get("preferred_payment_type"),
                                cpm_id=str(target) if target else None)
    if not pm.get("has_method"):
        attempt = _halt_attempt(conn, prior, invoice, wo_number, "email", balance,
                                pm.get("error", "no payment method"), extra=pm)
        return halt("no_payment_method",
                    f"no_payment_method ({pm.get('error', 'no PM on file')[:120]})",
                    attempt, error=pm.get("error"))

    # THE CHARGE — one call to the shared service.
    receipt_email = fetch_qbo_customer_email(customer_id, access_token, realm_id)
    r = charge_and_record(conn, build_intent(invoice, wo_number, pm, receipt_email),
                          access_token, realm_id)

    # Dispatch on the outcome — THIS workflow's policy.
    if r["status"] == "read_failed":
        return _result(qbo_invoice_id, "error", error=f"qbo_fetch_failed: {r['error']}")
    if r["status"] == "already_paid":  # paid between our read and the service's
        out = _send_only(conn, prior, invoice, wo_number, "email", 0,
                         "balance reached 0 before the charge fired (paid upstream)",
                         access_token, realm_id)
        _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
        return out
    if r["status"] == "uncertain":
        return _result(qbo_invoice_id, "uncertain", attempt_id=r["attempt_id"],
                       error=r["error"],
                       note="charge state unknown — reconcile_payments will resolve, "
                            "or retry safely (idempotency_key reused)")
    if r["status"] == "declined":
        return halt("charge_declined",
                    f"charge_declined ({(r.get('error') or 'declined')[:120]})",
                    {"id": r["attempt_id"]}, error=r["error"])
    if r["status"] == "payment_orphan":
        return halt("payment_orphan",
                    f"payment_orphan (charged ${float(r.get('amount') or 0):.2f}, "
                    f"ledger write failed; verify in QBO + Intuit before retrying)",
                    {"id": r["attempt_id"]}, charge_id=r["charge_id"],
                    amount=r["amount"], error=r["error"])

    # succeeded — deliver + verified-echo cache + done
    inv_email = deliver(conn, qbo_invoice_id, customer_id, access_token, realm_id)
    update_attempt(conn, r["attempt_id"], email_sent=r["receipt_sent"])
    _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=r["attempt_id"], charge_id=r["charge_id"],
                   qbo_payment_id=r["payment_id"], receipt_sent=r["receipt_sent"],
                   invoice_email_sent=inv_email["success"],
                   invoice_email_skipped=inv_email.get("skipped", False),
                   resumed=r.get("resumed"))


def _recover_orphan(conn, prior, invoice, wo_number, access_token, realm_id):
    """Human-verified orphan recovery: retry ONLY record_payment with the
    persisted charge_id (never charges again). charge_and_record refuses
    orphans by design — a blind record retry can double-record."""
    import json
    qbo_invoice_id = invoice["qbo_invoice_id"]
    customer_id = invoice.get("qbo_customer_id")

    charge_result = prior.get("charge_result") or {}
    if isinstance(charge_result, str):
        charge_result = json.loads(charge_result)
    charge_id = prior.get("charge_id") or charge_result.get("charge_id")
    if not charge_id:
        return _result(qbo_invoice_id, "error", attempt_id=str(prior["id"]),
                       error="orphan recovery requested but no charge_id on prior attempt")
    charge_result.setdefault("charge_id", charge_id)

    amount = float(prior["charge_amount"] or 0)
    pay = record_qbo_payment(customer_id, amount, charge_result, wo_number,
                             f"Auto-charge | WO# {wo_number} | Inv# {invoice.get('doc_number')}",
                             access_token, realm_id, [(qbo_invoice_id, amount)])
    if not pay["success"]:
        update_attempt(conn, prior["id"], status="payment_orphan",
                       error_message=f"orphan recovery: record_payment still failing: "
                                     f"{str(pay.get('error', ''))[:300]}")
        mark_invoice_needs_review(conn, qbo_invoice_id,
                                  f"payment_orphan (charged ${amount:.2f}, ledger retry still failing)")
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       attempt_id=str(prior["id"]), charge_id=charge_id, amount=amount,
                       error=pay.get("error"),
                       note="record_payment retry failed — verify in QBO/Intuit")

    update_attempt(conn, prior["id"], qbo_payment_id=pay["payment_id"])
    insert_webhook_expectation(conn, "Payment", pay["payment_id"])
    inv_email = deliver(conn, qbo_invoice_id, customer_id, access_token, realm_id)
    receipt = send_payment_receipt(pay["payment_id"], customer_id, access_token, realm_id)
    _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
    update_attempt(conn, prior["id"], status="succeeded", email_sent=receipt["success"],
                   raw_result=_dumps({"orphan_recovery": True, "payment": pay,
                                      "receipt": receipt, "invoice_email": inv_email}))
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=str(prior["id"]), charge_id=charge_id,
                   qbo_payment_id=pay["payment_id"], recovered_from="payment_orphan",
                   invoice_email_sent=inv_email["success"],
                   invoice_email_skipped=inv_email.get("skipped", False))


def _process_email_only(conn, prior, invoice, wo_number, customer_id,
                        access_token, realm_id):
    """preferred_payment_type='email' — the email IS the deliverable.
    Auto-retry up to EMAIL_RETRY_MAX."""
    qbo_invoice_id = invoice["qbo_invoice_id"]
    attempt = prior if (prior and prior["status"] == "pending") else create_attempt(
        conn, qbo_invoice_id, STAGE, invoice.get("doc_number"), "email",
        float(invoice.get("balance") or 0), False, wo_number=wo_number,
        payment_method=invoice.get("payment_method"))

    # Bump DueDate so a long-parked invoice doesn't arrive showing OVERDUE.
    # Best-effort — a failed PATCH never blocks the deliverable.
    due = bump_invoice_due_date_to_today(qbo_invoice_id, access_token, realm_id)
    if due.get("success") and not due.get("skipped"):
        insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
    print(f"  due-date: {due}")

    last_err = None
    for i in range(EMAIL_RETRY_MAX):
        email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
        if email["success"]:
            update_attempt(conn, attempt["id"], status="succeeded", email_sent=True,
                           raw_result=_dumps({"email": email, "attempts": i + 1}))
            if not email.get("skipped"):
                insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
            _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
            return _result(qbo_invoice_id, "succeeded", attempt_id=str(attempt["id"]),
                           sent_to=email.get("sent_to"), skipped=email.get("skipped", False))
        last_err = email.get("error")
        if i + 1 < EMAIL_RETRY_MAX:
            time.sleep(EMAIL_RETRY_BACKOFF_S)

    update_attempt(conn, attempt["id"], status="email_failed", error_message=last_err,
                   raw_result=_dumps({"attempts": EMAIL_RETRY_MAX, "last_error": last_err}))
    mark_invoice_needs_review(conn, qbo_invoice_id,
                              f"email_failed (after {EMAIL_RETRY_MAX} retries: {(last_err or 'unknown')[:120]})")
    return _result(qbo_invoice_id, "email_failed",
                   attempt_id=str(attempt["id"]), error=last_err)


# ── main: the event + the queue worker ───────────────────────────────────────
# Live batch runs go THROUGH billing.service_charge_queue (WORKFLOW_EXECUTION):
# enqueue the units (coalesced; interactive clicks at priority 1, backfill
# floods behind them), then drain until empty. Dry runs plan directly; force /
# recover_orphan runs stay direct (they must never apply force to unrelated
# queued units). trg_enqueue_service_charge also feeds the queue as invoices
# turn ready — a drain picks those up too.

CLAIM = """
UPDATE billing.service_charge_queue
SET started_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM billing.service_charge_queue
            WHERE finished_at IS NULL AND attempts < 3
            ORDER BY priority, received_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, qbo_invoice_id
"""


def _drain(conn, access_token, realm_id, sleep_ms, max_units=1000):
    stats, sample, drained = {}, [], 0
    while drained < max_units:
        row = _row(conn, CLAIM, ())
        conn.commit()
        if not row:
            break  # queue empty
        drained += 1
        qid = row["qbo_invoice_id"]
        try:
            res = process_one(conn, qid, access_token, realm_id)
            _exec(conn, "UPDATE billing.service_charge_queue "
                        "SET finished_at = now(), error = NULL WHERE id = %s", (row["id"],))
        except Exception as e:
            conn.rollback()
            res = _result(qid, "error", error=str(e)[:300])
            # stays open: re-claims until attempts >= 3, then dead-letters
            _exec(conn, "UPDATE billing.service_charge_queue "
                        "SET started_at = NULL, error = %s WHERE id = %s",
                  (str(e)[:300], row["id"]))
        status = res.get("status", "error")
        stats[status] = stats.get(status, 0) + 1
        if len(sample) < 20:
            sample.append(res)
        print(f"  [{drained}] {qid} -> {status}"
              + (f"  ({res.get('reason') or res.get('error') or ''})"
                 if status != 'succeeded' else ''))
        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)
    print(f"=== drained {drained}: {stats} ===")
    return {"status": "success", "drained": drained, "stats": stats,
            "sample": sample, "dry_run": False}


def main(qbo_invoice_id: str = None,
         qbo_invoice_ids: list = None,
         dry_run: bool = False,
         recover_orphan: bool = False,
         force: bool = False,
         bulk_all: bool = False,
         drain: bool = False,
         limit: int = None,
         sleep_ms: int = 800):
    """
    Modes: single (qbo_invoice_id, direct) | list (qbo_invoice_ids: live =
    enqueue at priority 1 + drain; dry = plan directly) | bulk_all (live =
    enqueue everything derived-ready + drain; dry = plan) | drain=True
    (just drain whatever is queued — backfill kicks).
    force / recover_orphan are direct-only recovery paths.
    """
    if not qbo_invoice_id and not qbo_invoice_ids and not bulk_all and not drain:
        return {"status": "error",
                "error": "pass qbo_invoice_id, qbo_invoice_ids=[...], bulk_all=True, or drain=True"}

    print(f"=== process_invoice (dry_run={dry_run}, recover_orphan={recover_orphan}, "
          f"force={force}, bulk_all={bulk_all}, drain={drain}) ===")
    conn = get_db_conn()
    set_rate_limiter(conn)  # ADR 008 §4: every QBO call claims
    try:
        access_token, realm_id = refresh_qbo_token()
        if qbo_invoice_id and not qbo_invoice_ids:
            return process_one(conn, qbo_invoice_id, access_token, realm_id,
                               dry_run=dry_run, recover_orphan=recover_orphan, force=force)

        # LIVE batch (no force): through the queue.
        if not dry_run and not force:
            if qbo_invoice_ids:
                _exec(conn, """INSERT INTO billing.service_charge_queue
                                 (qbo_invoice_id, priority)
                               SELECT unnest(%s::text[]), 1
                               ON CONFLICT (qbo_invoice_id)
                                 WHERE finished_at IS NULL DO NOTHING""",
                      (list(dict.fromkeys(qbo_invoice_ids)),))
            elif bulk_all:
                _exec(conn, """INSERT INTO billing.service_charge_queue (qbo_invoice_id)
                               SELECT s.qbo_invoice_id FROM billing.v_invoice_status s
                               WHERE s.derived_status = 'ready_to_process'
                               ON CONFLICT (qbo_invoice_id)
                                 WHERE finished_at IS NULL DO NOTHING""", ())
            return _drain(conn, access_token, realm_id, sleep_ms,
                          max_units=int(limit) if limit else 1000)

        # DRY RUN (plan directly) or FORCE (explicit human recovery, direct).
        if qbo_invoice_ids:
            targets = list(qbo_invoice_ids)
        else:
            cur = conn.cursor()
            cur.execute("SELECT i.qbo_invoice_id "
                        "FROM billing.v_invoice_status s "
                        "JOIN billing.invoices i USING (qbo_invoice_id) "
                        "WHERE s.derived_status = 'ready_to_process' "
                        "ORDER BY i.txn_date DESC NULLS LAST"
                        + (f" LIMIT {int(limit)}" if limit else ""))
            targets = [r[0] for r in cur.fetchall()]
            cur.close()

        print(f"Processing {len(targets)} invoice(s)")
        stats, sample = {}, []
        for i, qid in enumerate(targets):
            try:
                res = process_one(conn, qid, access_token, realm_id,
                                  dry_run=dry_run, recover_orphan=recover_orphan, force=force)
            except Exception as e:
                res = _result(qid, "error", error=str(e)[:300])
            status = res.get("status", "error")
            stats[status] = stats.get(status, 0) + 1
            if i < 20:
                sample.append(res)
            print(f"  [{i+1}/{len(targets)}] {qid} -> {status}"
                  + (f"  ({res.get('reason') or res.get('error') or ''})"
                     if status not in ('succeeded', 'dry_run_complete') else ''))
            if sleep_ms and i + 1 < len(targets):
                time.sleep(sleep_ms / 1000.0)
        print(f"=== done: {stats} ===")
        return {"status": "success", "total": len(targets), "stats": stats,
                "sample": sample, "dry_run": dry_run}
    finally:
        conn.close()
